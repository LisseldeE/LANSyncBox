"""
同步窗口
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
import os
import json
import uuid

from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
    QLabel, QPushButton, QTextEdit, QFrame, QSplitter, QMessageBox,
    QTableWidget, QTableWidgetItem, QProgressBar, QHeaderView, QApplication,
    QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, Signal, QEvent, QMetaObject, Q_ARG, Slot, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QIcon, QPixmap, QCursor, QPalette, QKeySequence, QShortcut
from pathlib import Path

from i18n import I18n
from config import Config, UserConfig
from ui.file_list_widget import FileListWidget
from ui.widgets import AnimatedButton, BUTTON_STYLES, ToggleSwitch
from ui.about_dialog import AboutDialog
from network.server import SyncServer
from utils.clean_queue import get_clean_queue
from network.client import SyncClient
from network.discovery import RoomResponder
from utils.transfer_queue import TransferQueue
from network.file_provider import FileProvider, pull_file


class SyncWindow(QMainWindow):
    """同步窗口"""
    
    # 信号
    closed = Signal()
    # 远程文件拉取进度（工作线程发射，经排队连接回主线程更新进度条）
    p2p_progress = Signal(str, int, int)  # (session_key, received_bytes, total_bytes)
    
    def __init__(self, is_host: bool, room_code: str, password: str = "", host_address: str = "", host_port: int = None, existing_client=None):
        super().__init__()
        self.is_host = is_host
        self.room_code = room_code
        self.password = password
        self.host_address = host_address
        self.host_port = host_port
        self._existing_client = existing_client  # 由 JoinRoomDialog 预验证成功的 client（可选，避免重复连接）
        
        # 获取房间文件夹
        self.room_folder = Config.get_room_folder(room_code)
        
        # 网络组件
        self.server = None
        self.client = None
        self.responder = None

        # 顶部拖拽放置区（快捷添加文件）：仅在房间连接就绪后创建
        self._drop_zone = None
        self._drag_detector = None   # Win32 全局拖拽会话检测器
        self._drag_loop = None       # 轮询钩子状态、驱动放置条滑入/滑出的定时器
        
        # 传输进度跟踪
        self._transfer_rows = {}  # 文件名 -> 行号映射
        self._cancelled_transfers = set()  # 已取消的文件名（忽略残留进度信号）
        
        # 传输队列管理器（限制同时传输5个文件）
        self.transfer_queue = TransferQueue(max_concurrent=5)

        # 局域网剪切板监控（房间连接成功后才使能；本端复制→上报主机分发）
        from utils.clipboard_monitor import ClipboardMonitor
        self._monitor = ClipboardMonitor(self)
        self._monitor.clipboard_committed.connect(self._on_local_clipboard_committed)
        # 文件/图片复制：复制端建会话并上报（交给本端 FileProvider 提供字节）
        self._monitor.file_copy.connect(self._on_local_file_copy)

        # 远程文件粘贴：Ctrl+V（仅当文本框获得焦点时不拦截）
        self._paste_shortcut = QShortcut(QKeySequence(Qt.CTRL | Qt.Key_V), self)
        self._paste_shortcut.activated.connect(self._paste_remote_files)

        # 关闭确认标志（避免 on_disconnect 确认后 close() 再次弹窗）
        self._close_confirmed = False

        # 主机端连接端详情浮层（延迟显示）
        self._latency_popup = None
        self._latency_popup_effect = None
        self._latency_popup_anim = None
        self._latency_fade_out = False
        self._latency_popup_hovered = False
        self._latency_open_timer = None
        self._latency_close_timer = None
        # 浮层定时刷新：后台接收线程把延迟写入 server.clients[*]['latency'] 字典缓冲，
        # 前台 GUI 用此 1 秒定时器读取字典重绘，避免高连接数下每秒数十次信号触发重绘。
        self._latency_popup_refresh_timer = None

        # 隐藏的日志记录（不在表格显示，但导出时包含）
        self._hidden_logs = []

        # 局域网剪切板-文件/图片分布式传输
        self._provider = None          # 本机 FileProvider（复制端目录服务）
        self.p2p_queue = TransferQueue(max_concurrent=5)  # 远程文件拉取队列（并发5）
        self._remote_files = None      # 本端"可用远程文件"元信息（最新复制覆盖旧）
        self._clipboard_rows = {}      # session_key -> 剪贴板进度行信息
        self._self_temp_images = []    # 本端复制图片时生成的临时 PNG（下次复制时清理）
        self.p2p_progress.connect(self._update_p2p_progress)

        self.init_ui()
        self.init_network()
    
    def init_ui(self):
        """初始化界面"""
        # 窗口设置
        self.setWindowTitle(I18n.tr('app_name'))
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
        
        # 房间号（可点击复制）
        self.room_label = QLabel(I18n.tr('room_info', code=self.room_code))
        self.room_label.setAlignment(Qt.AlignCenter)
        self.room_label.setCursor(Qt.PointingHandCursor)
        self.room_label.setToolTip(I18n.tr('click_to_copy_room'))
        self.room_label.mousePressEvent = self._copy_room_code
        info_layout.addWidget(self.room_label)

        # IP地址显示（点击可复制）
        self.ip_label = QLabel(self._get_local_ip_display())
        self.ip_label.setAlignment(Qt.AlignCenter)
        self.ip_label.setCursor(Qt.PointingHandCursor)
        self.ip_label.setToolTip(I18n.tr('click_to_copy_ip'))
        self.ip_label.setStyleSheet("color: #666; font-size: 11px;")
        self.ip_label.mousePressEvent = self._copy_ip_address
        info_layout.addWidget(self.ip_label)

        # 状态标签（主机端显示"已就绪 | 在线: X"，连接端显示"已连接/已断开"）
        if self.is_host:
            self.status_label = QLabel(f'<span style="color: green;">{I18n.tr("status_ready")}</span> | {I18n.tr("online_count")}: 0')
        else:
            self.status_label = QLabel(I18n.tr('status_connected'))
        self.status_label.setAlignment(Qt.AlignCenter)
        info_layout.addWidget(self.status_label)

        # 主机端：连接数标签安装事件过滤器，悬停时显示连接端详情浮层
        if self.is_host:
            self._latency_popup = None
            self.status_label.setAttribute(Qt.WA_Hover, True)
            self.status_label.installEventFilter(self)
        
        # 断开连接按钮
        disconnect_btn = AnimatedButton(I18n.tr('disconnect'))
        disconnect_btn.clicked.connect(self.on_disconnect)
        disconnect_btn.setStyleSheet(BUTTON_STYLES['danger'])
        info_layout.addWidget(disconnect_btn)

        left_layout.addWidget(info_frame)

        # 左侧下方：同步记录表格（使用 stretch=1 自动扩展）
        log_frame = QFrame()
        log_frame.setFrameShape(QFrame.StyledPanel)
        log_layout = QVBoxLayout(log_frame)
        log_layout.setContentsMargins(10, 10, 10, 10)
        log_layout.setSpacing(5)

        # 日志标题和导出按钮（水平布局）
        log_header_layout = QHBoxLayout()
        log_header_layout.setSpacing(10)

        log_title = QLabel(I18n.tr('transfer_log'))
        log_title.setStyleSheet("font-weight: bold;")
        log_header_layout.addWidget(log_title)

        log_header_layout.addStretch()

        # 导出日志按钮
        export_btn = QPushButton(I18n.tr('export_log'))
        export_btn.setFlat(True)
        export_btn.setCursor(Qt.PointingHandCursor)
        export_btn.setStyleSheet("""
            QPushButton {
                font-size: 12px;
                color: #495057;
                border: none;
                padding: 0px;
                background: transparent;
            }
            QPushButton:hover {
                color: #228be6;
            }
        """)
        export_btn.clicked.connect(self._export_log)
        log_header_layout.addWidget(export_btn)

        log_layout.addLayout(log_header_layout)
        
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
        self.records_table.setColumnWidth(1, 400)  # 信息列宽度，确保进度文本不被截断
        
        self.records_table.setAlternatingRowColors(True)
        self.records_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.records_table.setSelectionMode(QTableWidget.NoSelection)  # 无选择模式
        self.records_table.setFocusPolicy(Qt.NoFocus)  # 无焦点策略
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
            QTableWidget::item:selected {
                background: transparent;
            }
            QHeaderView::section {
                font-weight: bold;
                padding: 4px;
                border: none;
                border-bottom: 1px solid #dee2e6;
            }
        """)
        log_layout.addWidget(self.records_table)

        # 添加 log_frame 到左侧布局，使用 stretch=1 自动扩展
        left_layout.addWidget(log_frame, 1)

        splitter.addWidget(left_widget)
        
        # 右侧：文件列表
        self.file_list = FileListWidget(self.room_folder)
        # 连接文件操作信号
        self.file_list.file_added.connect(self.on_file_added)
        self.file_list.file_deleted.connect(self.on_file_deleted)
        self.file_list.file_renamed.connect(self.on_file_renamed)
        self.file_list.dir_created.connect(self.on_dir_created)
        # 设置取消传输回调（直接调用，避免 Qt 信号异步性问题）
        self.file_list.set_cancel_transfer_callback(self.on_cancel_transfer)
        # 手动同步按钮
        self.file_list.manual_sync_requested.connect(self.on_manual_sync_requested)
        self._sync_btn_locked = False  # 手动同步按钮 3 秒冷却锁
        self.file_list.set_sync_btn_enabled(False)  # 初始不可点，连接/在线后启用
        splitter.addWidget(self.file_list)
        
        # 设置分隔器比例
        splitter.setSizes([280, 720])
        
        main_layout.addWidget(splitter)
        
        # 底部状态栏（行高减小）
        bottom_frame = QFrame()
        bottom_frame.setFixedHeight(28)
        bottom_layout = QHBoxLayout(bottom_frame)
        bottom_layout.setContentsMargins(10, 2, 10, 2)

        # 同步文件夹路径（前缀显示版本号.串号，竖线分隔、垂直居中）
        folder_widget = QWidget()
        folder_layout = QHBoxLayout(folder_widget)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.setSpacing(6)

        serial_label = QLabel(Config.APP_VERSION_SERIAL)
        folder_layout.addWidget(serial_label, 0, Qt.AlignVCenter)

        # 竖线（用 VLine 绘制，固定高度保证垂直居中不越界）
        vline = QFrame()
        vline.setFrameShape(QFrame.VLine)
        vline.setFrameShadow(QFrame.Plain)
        vline.setFixedHeight(12)
        folder_layout.addWidget(vline, 0, Qt.AlignVCenter)

        path_label = QLabel(I18n.tr('sync_folder_path', path=str(self.room_folder)))
        path_label.setToolTip(str(self.room_folder))  # 长路径悬浮查看完整
        folder_layout.addWidget(path_label, 0, Qt.AlignVCenter)

        bottom_layout.addWidget(folder_widget)

        bottom_layout.addStretch()

        # 清理缓存文件开关
        clean_cache_label = QLabel(I18n.tr('clean_cache_label'))
        bottom_layout.addWidget(clean_cache_label)

        # 开关组件
        self.clean_cache_switch = ToggleSwitch()
        # 从配置加载开关状态
        clean_cache_enabled = UserConfig.get_clean_cache_enabled()
        self.clean_cache_switch.setChecked(clean_cache_enabled, animate=False)
        # 设置悬浮提示
        self.clean_cache_switch.setToolTip(I18n.tr('clean_cache_tooltip'))
        # 连接状态改变信号
        self.clean_cache_switch.stateChanged.connect(self._on_clean_cache_changed)
        bottom_layout.addWidget(self.clean_cache_switch)

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
            self.server.file_receive_cancelled.connect(self.on_remote_file_cancelled)
            self.server.file_deleted.connect(self.on_remote_file_deleted)
            self.server.file_renamed.connect(self.on_remote_file_renamed)
            self.server.dir_created.connect(self.on_remote_dir_created)
            self.server.log_message.connect(self.add_log_from_network)
            # 主机端发送文件的进度信号
            self.server.file_send_progress.connect(self.on_file_send_progress)
            self.server.file_sent.connect(self.on_file_sent)
            # 主机端转发文件的进度信号（包含目标IP）
            self.server.file_forward_progress.connect(self.on_file_forward_progress)
            self.server.file_forward_sent.connect(self.on_file_forward_sent)
            # 主机端转发文件被取消（目标连接端取消接收 / 转发中断）
            self.server.file_forward_cancelled.connect(self.on_file_forward_cancelled)
            # 主机端连接端延迟更新信号
            self.server.latency_updated.connect(self.on_client_latency_updated)
            # 主机端接收剪切板内容（写主机自身系统剪贴板）
            self.server.clipboard_received.connect(self.on_server_clipboard_received)
            # 主机端收到文件会话通知（展示远程文件胶囊；文件字节不经主机）
            self.server.files_notify_received.connect(self.on_files_notify)

            # 先启动房间响应服务（占用发现端口）
            self.responder = RoomResponder(self)
            if not self.responder.start(self.room_code):
                self._add_record("启动发现服务失败", "错误", "")
                return
            self._add_record(f"发现服务 端口: {self.responder.discovery_port}", "启动", "")

            # 然后启动服务器（避开已占用的发现端口）
            if self.server.start(exclude_port=self.responder.discovery_port):
                self._add_record(f"服务器 端口: {self.server.port}", "启动", "")
                # 更新发现服务的同步端口
                self.responder.port = self.server.port
                # 房间就绪：开启局域网剪切板分发 + 启动本端目录服务（复制端 serve 用）
                self._start_provider()
                self._monitor.set_enabled(True)
            else:
                self._add_record("启动失败", "错误", "")
                self.responder.stop()
        else:
            # 客户端：连接到服务器
            if self._existing_client is not None:
                # 复用 JoinRoomDialog 预验证成功的 client（已建立连接并通过验证）
                self.client = self._existing_client
                self._existing_client = None
            else:
                # 自行创建并连接
                self.client = SyncClient(self.room_code, self.password)
            self.client.connected.connect(self.on_connected)
            self.client.disconnected.connect(self.on_disconnected)
            self.client.error_occurred.connect(self.on_network_error)
            self.client.auth_failed.connect(self.on_auth_failed)
            self.client.file_receive_start.connect(self.on_file_receive_start)
            self.client.file_receive_progress.connect(self.on_file_receive_progress)
            self.client.file_received.connect(self.on_remote_file_received)
            self.client.file_receive_cancelled.connect(self.on_remote_file_cancelled)
            self.client.file_deleted.connect(self.on_remote_file_deleted)
            self.client.file_renamed.connect(self.on_remote_file_renamed)
            self.client.dir_created.connect(self.on_remote_dir_created)
            self.client.log_message.connect(self.add_log_from_network)
            # 客户端发送文件的进度信号
            self.client.file_send_progress.connect(self.on_file_send_progress)
            self.client.file_sent.connect(self.on_file_sent)
            # 客户端文件列表接收信号
            self.client.file_list_received.connect(self.on_file_list_received)
            # 手动同步相关信号
            self.client.sync_requested.connect(self.on_sync_requested)
            self.client.sync_result.connect(self.on_sync_result)
            # 连接端自身延迟更新信号
            self.client.latency_updated.connect(self.on_latency_updated)
            # 连接端接收剪切板内容（写本端系统剪贴板）
            self.client.clipboard_received.connect(self.on_client_clipboard_received)
            # 连接端收到主机转发的文件会话通知（展示远程文件胶囊）
            self.client.files_notify_received.connect(self.on_files_notify)

            # 连接到服务器（复用模式下 client 已验证通过，直接记录日志）
            host = self.host_address or "127.0.0.1"
            port = self.host_port or Config.DEFAULT_PORT
            if self.client.authenticated:
                self._add_record(f"{host}:{port}", "连接", "")
                self.on_connected()
            elif self.client.connect_to_server(host, port):
                self._add_record(f"{host}:{port}", "连接", "")
            else:
                self._add_record("连接失败", "错误", "")
    
    def add_log_from_network(self, message: str):
        """从网络层添加日志（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "add_log", Qt.QueuedConnection,
                                 Q_ARG(str, "网络"), Q_ARG(str, message))

    def _on_local_clipboard_committed(self, mime_type: str, data: bytes):
        """本端系统剪贴板新增文本：按主机/连接端身份上报分发"""
        if mime_type != "text" or not data:
            return
        if self.is_host:
            if self.server:
                self.server.send_clipboard(mime_type, data)
        else:
            if self.client and self.client.authenticated:
                self.client.send_clipboard(mime_type, data)

    def _apply_system_clipboard(self, mime_type: str, data: bytes) -> bool:
        """将接收到的剪切板文本写入本端系统剪贴板，并记录摘要以抑制回环。"""
        if mime_type != "text" or not data:
            return False
        # 先记录摘要，使随后的 dataChanged 被识别为本端写回，不重复上报
        self._monitor.set_written_hash(mime_type, data)
        from utils.clipboard_monitor import ClipboardMonitor
        written = ClipboardMonitor.apply_to_clipboard(mime_type, data)
        if not written:
            return False
        self.add_log("剪切板", I18n.tr("clipboard_text_synced"))
        return True

    def on_server_clipboard_received(self, mime_type: str, data: bytes):
        """主机端收到连接端上报的剪切板内容：写入主机自身剪贴板"""
        self._apply_system_clipboard(mime_type, data)

    def on_client_clipboard_received(self, mime_type: str, data: bytes):
        """连接端收到主机分发的剪切板内容：写入本端系统剪贴板"""
        self._apply_system_clipboard(mime_type, data)

    # ---- 局域网剪切板：文件/图片 分布式传输 ----

    def _start_provider(self):
        """启动本机 FileProvider（复制端目录服务），房间就绪/连接后调用。"""
        if self._provider is not None:
            return
        self._provider = FileProvider(self)
        if not self._provider.start():
            self._add_record(I18n.tr('p2p_provider_start_fail', port=self._provider.DEFAULT_START_PORT or ""), "错误", "")

    def _stop_provider(self):
        """停止本机 FileProvider，并清理其登记的全部会话。"""
        if self._provider is not None:
            self._provider.stop()
            self._provider = None

    def _remove_temp_self_images(self):
        """删除本端复制图片时生成的临时 PNG。"""
        for path in self._self_temp_images:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
        self._self_temp_images = []

    def _on_local_file_copy(self, entries: list):
        """本端复制了文件/图片：建会话、登记到本机目录服务、上报主机分发。

        Args:
            entries: [{'name', 'path'}, ...]（图片已由 monitor 存为临时 PNG）
        """
        if self._provider is None or not entries:
            return
        # 网络未就绪时不投递
        if self.is_host:
            if self.server is None:
                return
        else:
            if self.client is None or not self.client.authenticated:
                return

        # "最新为主"：新复制顶掉旧会话（旧 temp 图片只在图片复制时清理）
        session_id = uuid.uuid4().hex
        token = uuid.uuid4().hex

        files_map = {}
        files_meta = []
        new_temp = []  # 本次复制涉及的临时图片
        total = 0
        for ent in entries:
            name = os.path.basename(ent['path'])
            path = ent['path']
            if not os.path.isfile(path):
                continue
            files_map[name] = path
            size = os.path.getsize(path)
            files_meta.append({'name': name, 'size': size})
            total += size
            # 追踪本端生成的临时图片，下次复制/关闭时清理
            if 'ClipboardImages' in path:
                new_temp.append(path)
        if not files_map:
            return

        # 新会话顶掉旧会话：清理上一轮遗留的临时图片，再登记本轮
        self._remove_temp_self_images()
        self._self_temp_images = new_temp

        self._provider.register_session(session_id, token, files_map)

        notify = {
            'session_id': session_id,
            'token': token,
            'files': files_meta,
            'source_ip': self._provider.host,
            'source_port': self._provider.port,
        }
        if self.is_host:
            self.server.send_files_notify(notify)
        else:
            self.client.send_files_notify(notify)
        self.add_log("剪切板", I18n.tr('clipboard_files_copied', count=len(files_meta)))

    def on_files_notify(self, content: bytes):
        """收到远程文件会话通知（主机/连接端共用）：更新"可用远程文件"（最新为主）。"""
        if not content:
            return
        try:
            notify = json.loads(content.decode('utf-8'))
        except Exception:
            return
        if not notify.get('session_id') or not notify.get('files'):
            return
        # 最新复制覆盖旧的可用远程文件状态
        self._remote_files = notify
        names = " · ".join(f"{f.get('name')}" for f in notify['files'])
        self.add_log("剪切板", I18n.tr('clipboard_files_available', names=names))

    def _paste_remote_files(self):
        """Ctrl+V：把"可用远程文件"拉取到当前浏览目录。

        仅当存在可用远程文件且焦点不在文本框时触发；否则放行文本粘贴。
        """
        focus = QApplication.focusWidget()
        from PySide6.QtWidgets import QLineEdit, QTextEdit
        if isinstance(focus, (QLineEdit, QTextEdit)):
            return  # 输入框中正常粘贴文本，不劫持
        notify = self._remote_files
        if not notify or not notify.get('files'):
            return
        session_id = notify['session_id']
        token = notify['token']
        source_ip = notify.get('source_ip', '127.0.0.1')
        source_port = int(notify.get('source_port', 0) or 0)
        if source_port <= 0:
            return

        target_dir = self.file_list.current_path
        try:
            os.makedirs(target_dir, exist_ok=True)
        except OSError:
            self.add_log("剪切板", I18n.tr('p2p_paste_dir_fail'))
            return

        files = notify['files']
        count = len(files)
        total_bytes = sum(int(f.get('size', 0)) for f in files)
        self.add_log("剪切板", I18n.tr('clipboard_files_dispatching', count=count))
        self._add_p2p_progress(session_id, count, total_bytes)

        for f in files:
            name = os.path.basename(f.get('name', ''))
            if not name:
                continue
            self.p2p_queue.add_task(
                'p2p_pull', self._pull_remote_file, name,
                notify, name, target_dir, session_id,
            )

    def _pull_remote_file(self, stop_event, notify: dict, name: str, target_dir: str, session_key: str):
        """远程单文件拉取（TransferQueue 工作线程）。

        失败不重发（PRD）：记日志、不做重试；临时文件由 pull_file 内部清理。
        """
        if stop_event.is_set():
            return
        dest = os.path.join(target_dir, name)
        sid = notify['session_id']
        token = notify['token']
        try:
            ok, total, err = pull_file(
                notify.get('source_ip', '127.0.0.1'),
                int(notify.get('source_port', 0)),
                sid, token, name, dest,
                progress_cb=lambda recv, size: self.p2p_progress.emit(session_key, recv, size),
            )
        except Exception as e:
            ok, total, err = False, 0, str(e)
        if ok:
            self._log_worker("剪切板", I18n.tr('clipboard_file_received', name=name))
        else:
            self._log_worker("剪切板", I18n.tr('clipboard_file_pull_fail', name=name, msg=err))

    def _log_worker(self, section: str, message: str):
        """工作线程中安全地追加日志（排队回主线程）。"""
        QMetaObject.invokeMethod(self, "add_log", Qt.QueuedConnection,
                                 Q_ARG(str, section), Q_ARG(str, message))

    # ---- 远程文件进度条（淡蓝色，钉在已传输进度条下方） ----

    def _add_p2p_progress(self, session_key: str, count: int, total_bytes: int):
        """为一次"投递N个文件"新增一行淡蓝进度条。"""
        display = I18n.tr('clipboard_deliver_count', count=count)
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)
        action_item = QTableWidgetItem(I18n.tr('clipboard_deliver'))
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(row_count, 0, action_item)

        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setFormat(f"{display} - 0% (0/{(total_bytes / 1024 / 1024):.1f}M)")
        bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        bar.setStyleSheet("""
            QProgressBar { border: none; text-align: left; background-color: transparent; }
            QProgressBar::chunk { background-color: #74c0fc; }
        """)
        self.records_table.setCellWidget(row_count, 1, bar)
        self.records_table.setRowHeight(row_count, 25)
        self._clipboard_rows[session_key] = {'row': row_count, 'count': count}
        self.records_table.scrollToBottom()
        self._trim_history()

    def _update_p2p_progress(self, session_key: str, received: int, total: int):
        """主线程更新淡蓝进度条（来自工作线程的排队信号）。"""
        info = self._clipboard_rows.get(session_key)
        if not info:
            return
        bar = self.records_table.cellWidget(info['row'], 1)
        if not bar or not isinstance(bar, QProgressBar):
            return
        percent = int(received / total * 100) if total > 0 else 0
        bar.setValue(percent)
        cur_m = received / 1024 / 1024
        tot_m = total / 1024 / 1024
        bar.setFormat(f"{I18n.tr('clipboard_deliver_count', count=info['count'])} - {percent}% ({cur_m:.1f}/{tot_m:.1f}M)")
    
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
            # 主机端显示"已就绪 | 在线: X"，"已就绪"为绿色，其余为系统默认颜色
            self.status_label.setText(f'<span style="color: green;">{I18n.tr("status_ready")}</span> | {I18n.tr("online_count")}: {count}')
            # 在线人数变化时刷新手动同步按钮可用性（在锁外调用，避免非可重入锁死锁）
            self._update_sync_btn_state()

    def _on_status_label_hover(self, enter: bool):
        """主机端连接数标签悬停：显示/隐藏连接端详情浮层"""
        if not self.is_host:
            return
        if enter:
            self._show_latency_popup()
        else:
            self._hide_latency_popup()

    def _latency_popup_content(self) -> str:
        """生成主机端浮层的 HTML 内容：每行「IP    延迟 ms」两列对齐。

        用 HTML 表格实现：IP 左列、延迟右列右对齐，消除空格混排产生的混淆；
        延迟值前加状态圆点并用阈值色，使延迟一眼可辨。无连接端时显示提示。
        """
        with self.server._lock:
            entries = [(cid, info) for cid, info in list(self.server.clients.items())
                       if info.get('authenticated')]
        if not entries:
            return I18n.tr('no_clients_connected')
        rows = []
        for cid, info in entries:
            ip = info.get('ip') or cid.split(':')[0]
            lat = info.get('latency')
            if lat is not None:
                ms = int(round(lat))
                color = self._latency_color(ms)
                lat_str = f'<span style="color: {color};">{ms} {I18n.tr("latency_ms")}</span>'
                dot = f'<span style="color: {color};">●</span> '
            else:
                lat_str = I18n.tr('latency_unknown')
                dot = f'<span style="color: #8c8cf0;">●</span> '
            rows.append(
                f"<tr><td style='padding-right: 24px;'>{ip}</td>"
                f"<td align='right' style='white-space: nowrap;'>{dot}{lat_str}</td></tr>"
            )
        return (f"<table style='margin: 0;'>{''.join(rows)}</table>")

    def _show_latency_popup(self):
        """在连接数标签鼠标位置显示连接端详情浮层"""
        if not self.is_host or self.server is None:
            return
        if self._latency_popup is None:
            # 作为主窗口的子控件（与同步界面 toast 完全一致的渲染方式），
            # 避免顶层窗口自带方形底板导致"圆角卡+方底"。
            self._latency_popup = QLabel(self)
            pal = self.window().palette() if self.window() is not None else QApplication.palette()
            win_color = pal.color(QPalette.Window)
            luminance = win_color.red() * 0.299 + win_color.green() * 0.587 + win_color.blue() * 0.114
            if luminance > 128:  # 浅色主题
                bg, fg, border = QColor(255, 255, 255), QColor(34, 38, 42), QColor(140, 140, 146)
            else:  # 深色主题
                bg, fg, border = QColor(58, 60, 64), QColor(244, 244, 244), QColor(108, 108, 114)
            self._latency_popup.setStyleSheet(
                f"QLabel {{"
                f"  background-color: {bg.name()};"
                f"  color: {fg.name()};"
                f"  border: 1px solid {border.name()};"
                f"  border-radius: 8px;"
                f"  padding: 8px 14px;"
                f"  font-size: 13px;"
                f"}}"
            )
            self._latency_popup.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            self._latency_popup.setTextFormat(Qt.RichText)
            # 浮层对鼠标事件透明，避免它抢走状态标签上的悬停/离开事件；
            # 否则浮层出现瞬间，状态标签收到 Leave → 浮层立即消失。
            self._latency_popup.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            # 渐入渐出效果（子控件不能用 windowOpacity，需用 QGraphicsOpacityEffect）
            self._latency_popup_effect = QGraphicsOpacityEffect()
            self._latency_popup_effect.setOpacity(1.0)
            self._latency_popup.setGraphicsEffect(self._latency_popup_effect)
            self._latency_popup_anim = QPropertyAnimation(self._latency_popup_effect, b"opacity", self)
            self._latency_popup_anim.setDuration(150)
            self._latency_popup_anim.setEasingCurve(QEasingCurve.OutQuad)
            # 动画 finished 只连接一次（用 flag 区分渐入/渐出），避免 hide() 反复 append 连接
            self._latency_popup_anim.finished.connect(self._on_latency_popup_anim_finished)
        self._latency_popup.setText(self._latency_popup_content())
        self._latency_popup.adjustSize()
        self._latency_popup.raise_()
        # 固定定位：居中显示在"在线人数"状态标签下方（窗口内坐标，随窗口移动/缩放保持相对位置，
        # 且不受屏幕 DPI/缩放比例影响产生偏移）
        anchor = self.status_label.mapTo(self, self.status_label.rect().topLeft())
        x = anchor.x() + (self.status_label.width() - self._latency_popup.width()) // 2
        y = anchor.y() + self.status_label.height() + 6
        self._latency_popup.move(x, y)
        # 停止未完成的关闭定时器（鼠标重新进入时取消即将的隐藏）
        if self._latency_close_timer is not None and self._latency_close_timer.isActive():
            self._latency_close_timer.stop()
        # 若正在渐出，直接中断并回到完全不透明，便于立即反向渐入
        self._latency_popup_anim.stop()
        self._latency_popup_effect.setOpacity(1.0)
        self._latency_fade_out = False
        if not self._latency_popup.isVisible():
            self._latency_popup.show()
            self._latency_popup_anim.setStartValue(0.0)
            self._latency_popup_anim.setEndValue(1.0)
            self._latency_popup_anim.start()
        # 浮层可见期间启动 1 秒定时刷新（从字典缓冲读取重绘）
        if self._latency_popup_refresh_timer is None:
            self._latency_popup_refresh_timer = QTimer(self)
            self._latency_popup_refresh_timer.setInterval(1000)
            self._latency_popup_refresh_timer.timeout.connect(self._refresh_latency_popup_from_buffer)
        self._latency_popup_refresh_timer.start()

    def _refresh_latency_popup_from_buffer(self):
        """前台定时刷新：从 server.clients 字典缓冲读取各连接端延迟并重绘浮层。
        
        后台各接收线程负责把延迟写入字典，本方法只读并重建浮层内容，
        将 GUI 重绘收敛为每秒一次，规避高连接数下高频信号触发重绘。
        """
        popup = self._latency_popup
        if popup is None or not popup.isVisible():
            return
        popup.setText(self._latency_popup_content())
        popup.adjustSize()

    def _on_latency_popup_anim_finished(self):
        """浮层动画结束：仅当是渐出时真正隐藏（渐入结束不隐藏）"""
        if self._latency_fade_out:
            self._latency_fade_out = False
            self._latency_popup.hide()

    def _hide_latency_popup(self):
        """隐藏主机端连接端详情浮层（渐出后隐藏）。

        使用延迟定时器：鼠标短暂移出又立刻移入时不误隐藏，只有确认
        离开后才执行渐出，解决"移出移入呼不出面板"的问题。
        """
        if self._latency_close_timer is None:
            self._latency_close_timer = QTimer(self)
            self._latency_close_timer.setSingleShot(True)
            self._latency_close_timer.timeout.connect(self._do_hide_latency_popup)
        # 重启延迟关闭计时器：极短防抖，光标扫过标签边缘不误收，又接近即时收起
        self._latency_close_timer.start(100)

    def _do_hide_latency_popup(self):
        """延迟确认后真正执行渐出隐藏"""
        popup = self._latency_popup
        if popup is None or not popup.isVisible():
            return
        # 浮层将隐藏，停止 1 秒定时刷新，避免空转
        if self._latency_popup_refresh_timer is not None:
            self._latency_popup_refresh_timer.stop()
        anim = self._latency_popup_anim
        effect = self._latency_popup_effect
        if anim is not None and effect is not None:
            anim.stop()
            self._latency_fade_out = True
            anim.setStartValue(effect.opacity())
            anim.setEndValue(0.0)
            anim.start()
        else:
            popup.hide()

    def eventFilter(self, obj, event):
        """事件过滤器：捕获主机端连接数标签的悬停事件（带防抖）"""
        if self.is_host and obj is self.status_label:
            etype = event.type()
            if etype == QEvent.Type.Enter:
                self._latency_popup_hovered = True
                self._show_latency_popup()
                return False
            elif etype == QEvent.Type.Leave:
                self._latency_popup_hovered = False
                self._hide_latency_popup()
                return False
        return super().eventFilter(obj, event)

    def on_client_latency_updated(self, client_id: str, rtt_ms: float):
        """主机端：某连接端延迟更新（信号）。

        后台接收线程已把延迟写入 server.clients[*] 字典缓冲，浮层由
        _latency_popup_refresh_timer（1 秒定时）读取字典重绘，此处不再直接
        重绘 GUI，避免高连接数下每秒数十次信号触发界面刷新。
        """
        pass

    def _latency_color(self, ms: float) -> str:
        """根据延迟阈值返回对应颜色。
        绿 `<50` 与"已连接/已就绪"绿色一致(#008000)；橙 `<200`、红 `>=200` 与主界面缓存大小配色一致(#ff922b/#f03e3e)。
        """
        if ms < 50:
            return '#008000'
        if ms < 200:
            return '#ff922b'
        return '#f03e3e'

    def on_latency_updated(self, rtt_ms: float):
        """连接端：自身延迟更新，在"已连接"旁显示延迟"""
        if self.is_host:
            return
        ms = int(round(rtt_ms))
        color = self._latency_color(ms)
        self.status_label.setText(f'<span style="color: green;">{I18n.tr("status_connected")}</span> · <span style="color: {color};">{ms} {I18n.tr("latency_ms")}</span>')

    def _refresh_client_status_label(self):
        """重建连接端状态标签：保留已获取的延迟值（若无延迟则显示 -- ms）。

        连接端在每次文件传输完成等时机都会把状态标签重置为纯"已连接"，
        这里统一重建为带延迟的文本，避免延迟短暂消失又闪烁回来。
        """
        if self.is_host:
            return
        lat = self.client.latency if (self.client is not None) else None
        if lat is not None:
            ms = int(round(lat))
            color = self._latency_color(ms)
            text = f'<span style="color: green;">{I18n.tr("status_connected")}</span> · <span style="color: {color};">{ms} {I18n.tr("latency_ms")}</span>'
        else:
            text = f'<span style="color: green;">{I18n.tr("status_connected")}</span> · {I18n.tr("latency_unknown")} {I18n.tr("latency_ms")}'
        self.status_label.setText(text)

    def on_connected(self):
        """连接成功"""
        if self.is_host:
            # 主机端显示"已就绪 | 在线: X"，"已就绪"为绿色，其余为系统默认颜色
            self.status_label.setText(f'<span style="color: green;">{I18n.tr("status_ready")}</span> | {I18n.tr("online_count")}: 0')
        else:
            # 连接端显示"已连接"，延迟初始为 -- ms，收到首个 PING 回包后再刷新为实际值
            self.status_label.setText(f'<span style="color: green;">{I18n.tr("status_connected")}</span> · {I18n.tr("latency_unknown")} {I18n.tr("latency_ms")}')
            # 连接端连接成功：上报文件列表，触发首次全量差异同步
            self._perform_full_sync()
            # 连接就绪：开启局域网剪切板分发 + 启动本端目录服务（复制端 serve 用）
            self._start_provider()
            self._monitor.set_enabled(True)
        
        # 更新手动同步按钮可用状态
        self._update_sync_btn_state()
        # 房间就绪：启用顶部拖拽放置区（快捷添加文件到当前同步列表/根目录）
        self._init_drop_zone()

    def _init_drop_zone(self):
        """创建顶部快捷放置条与全局拖拽检测器，启用桌面任意位置拖入文件。

        放置条本身是独立置顶、非穿透的 OLE drop-target；是否弹条由全局低级鼠标钩子
        （DragDetector）轮询驱动，添加的文件交给当前列表（根目录即房间目录）。
        """
        if self._drop_zone is not None:
            return
        from ui.drop_zone import DropZone
        from ui.drag_detector import DragDetector
        self._drop_zone = DropZone(add_callback=self.file_list.add_files)
        # 胶囊若已集成（或测试中注入），传入以便放置条避让悬浮胶囊
        capsule = getattr(self, "_capsule", None)
        self._drop_zone.set_capsule(capsule)
        self._drop_zone.show()

        # 全局拖拽会话检测器 + 轮询驱动
        self._drag_detector = DragDetector()
        self._drag_detector.start()
        from PySide6.QtCore import QTimer
        self._drag_loop = QTimer(self)
        self._drag_loop.setInterval(30)
        self._drag_loop.timeout.connect(lambda: self._drop_zone.pump(self._drag_detector))
        self._drag_loop.start()

    def _perform_full_sync(self):
        """连接端执行一次全量差异同步：上报本地文件列表给主机端仲裁

        初次加入、自动重连、手动同步、响应主机端 SYNC_REQUEST 均复用到此方法。
        主机端收到后做差异仲裁并回传同步结果（列表一致/补齐差异项）。
        """
        if not self.client or not self.client.authenticated:
            return
        from sync.file_manager import FileManager
        from pathlib import Path

        file_manager = FileManager(Path(self.room_folder))
        local_file_list = file_manager.get_file_list_for_sync()
        local_empty_dirs = file_manager.get_empty_directory_list()
        self._send_file_list_to_server(local_file_list, local_empty_dirs)
    
    def _send_file_list_to_server(self, file_list: list, empty_dirs: list = None):
        """发送文件列表给主机端
        
        Args:
            file_list: 文件列表，格式为 [{"filename": "test.txt", "size": 1024, "mtime": 1234567890.123}, ...]
            empty_dirs: 空目录列表，格式为 ["subdir1", "subdir1/subdir2", ...]

        注意：文件列表内容以 {"files": [...], "empty_dirs": [...]} 结构上报，
        让主机端能感知连接端的空目录并同步补建。
        """
        if not self.client or not self.client.authenticated:
            return
        
        # 发送文件列表响应消息（实际上是发送自己的文件列表）
        import json
        content = json.dumps({
            'files': file_list,
            'empty_dirs': empty_dirs or []
        }).encode('utf-8')
        
        from network.protocol import Protocol, MessageType
        message = Protocol.pack_message(MessageType.FILE_LIST_RESP, '', len(content), False, content)
        self.client.send_bytes(message)
        
        self._add_record("", "发送", f"发送文件列表: {len(file_list)} 个文件")
    
    def on_auth_failed(self, message: str):
        """验证失败"""
        # 显示错误提示
        QMessageBox.critical(self, I18n.tr('auth_failed'), I18n.tr('auth_failed_msg', msg=message))

        # 关闭窗口（标记已确认，避免触发 closeEvent 的"确认离开"弹窗）
        self._close_confirmed = True
        self.close()
    
    def on_disconnected(self):
        """断开连接"""
        self._add_record("", I18n.tr('disconnected'), "")
        self.status_label.setText(I18n.tr('status_disconnected'))
        self.status_label.setStyleSheet("color: red;")
        # 断开后手动同步不可用
        self._update_sync_btn_state()

    def on_manual_sync_requested(self):
        """手动同步按钮：请求全量差异同步

        连接端：重新上报文件列表给主机端仲裁；
        主机端：通知所有在线连接端各自重新上报并补齐。
        """
        if self._sync_btn_locked:
            return
        self._sync_btn_locked = True
        self._update_sync_btn_state()
        # 日志：操作列=操作，信息列=手动同步
        self._add_record(I18n.tr('manual_sync'), I18n.tr('manual_sync_operation'))
        # 3 秒后恢复可点击（重新评估连接/在线状态）
        from PySide6.QtCore import QTimer
        QTimer.singleShot(3000, self._release_sync_btn)

        if self.is_host:
            if self.server:
                self.server.request_sync_all()
        else:
            self._perform_full_sync()

    def _release_sync_btn(self):
        """手动同步按钮冷却结束"""
        self._sync_btn_locked = False
        self._update_sync_btn_state()

    def on_sync_requested(self):
        """收到主机端 SYNC_REQUEST：触发一次全量差异同步（仅连接端）"""
        if not self.is_host:
            self._perform_full_sync()

    def on_sync_result(self, has_diff: bool):
        """收到主机端同步结果：显示"列表一致/正在补齐差异项"通知"""
        if has_diff:
            self.file_list.show_global_notification(I18n.tr('manual_sync_diff'))
        else:
            self.file_list.show_global_notification(I18n.tr('manual_sync_consistent'))

    def _update_sync_btn_state(self):
        """根据连接状态更新手动同步按钮可用性"""
        if self._sync_btn_locked:
            self.file_list.set_sync_btn_enabled(False)
            return
        if self.is_host:
            # 主机端：有在线连接端才可点击
            online = 0
            if self.server:
                with self.server._lock:
                    online = len([c for c in self.server.clients.values() if c['authenticated']])
            self.file_list.set_sync_btn_enabled(online > 0)
        else:
            # 连接端：已连接且已验证才可点击
            enabled = bool(self.client and self.client.authenticated)
            self.file_list.set_sync_btn_enabled(enabled)
    
    def on_file_list_received(self, remote_file_list: list):
        """收到文件列表响应（连接端）
        
        Args:
            remote_file_list: 远程文件列表，格式为 [{"filename": "test.txt", "size": 1024, "mtime": 1234567890.123}, ...]
        """
        # 获取本地文件列表
        from sync.file_manager import FileManager
        from pathlib import Path
        
        file_manager = FileManager(Path(self.room_folder))
        local_file_list = file_manager.get_file_list_for_sync()
        
        # 对比文件列表，找出需要同步的文件
        files_to_request = self._compare_file_lists(local_file_list, remote_file_list)
        
        if files_to_request:
            self._add_record("", "同步", f"需要同步 {len(files_to_request)} 个文件")
            
            # 将文件请求加入传输队列
            for filename in files_to_request:
                self._request_file_from_server(filename)
        else:
            self._add_record("", "同步", "无需同步")
    
    def _compare_file_lists(self, local_files: list, remote_files: list) -> list:
        """对比文件列表，找出需要请求的文件
        
        Args:
            local_files: 本地文件列表
            remote_files: 远程文件列表
        
        Returns:
            需要请求的文件名列表
        """
        # 创建本地文件字典（文件名 -> 文件信息）
        local_dict = {f['filename']: f for f in local_files}
        
        # 创建远程文件字典（文件名 -> 文件信息）
        remote_dict = {f['filename']: f for f in remote_files}
        
        # 找出需要请求的文件
        files_to_request = []
        
        for filename, remote_info in remote_dict.items():
            if filename not in local_dict:
                # 本地缺失的文件，需要请求
                files_to_request.append(filename)
            else:
                # 文件存在，比较大小和修改时间
                local_info = local_dict[filename]
                if remote_info['size'] != local_info['size']:
                    # 文件大小不同，需要同步（请求远程版本）
                    files_to_request.append(filename)
                elif remote_info['mtime'] > local_info['mtime']:
                    # 远程文件更新，需要请求
                    files_to_request.append(filename)
        
        return files_to_request
    
    def _request_file_from_server(self, filename: str):
        """从服务器请求文件（加入传输队列）
        
        Args:
            filename: 文件名（相对路径）
        """
        if not self.client:
            return
        
        # 添加到传输队列
        self.transfer_queue.add_task(
            'file_request',
            self._do_request_file,
            filename,
            filename
        )
    
    def _do_request_file(self, stop_event: threading.Event, filename: str):
        """实际执行：请求文件
        
        Args:
            stop_event: 停止标志
            filename: 文件名（相对路径）
        """
        # 检查是否需要停止
        if stop_event.is_set():
            return
        
        # 请求文件
        if self.client and self.client.authenticated:
            self.client.request_file(filename)
    
    def on_network_error(self, error: str):
        """网络错误"""
        self.add_log("错误", error)
    
    def _copy_room_code(self, event):
        """复制房间号到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(self.room_code)

        # 显示提示
        self.room_label.setToolTip(I18n.tr('copied'))
        QTimer.singleShot(2000, lambda: self.room_label.setToolTip(I18n.tr('click_to_copy_room')))

    def _get_local_ip_display(self) -> str:
        """获取本机IP地址并格式化显示"""
        import socket
        try:
            # 创建临时socket获取本机IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return f"{I18n.tr('ip_prefix')}{local_ip}"
        except Exception:
            return I18n.tr('ip_unknown')

    def _copy_ip_address(self, event):
        """复制IP地址到剪贴板"""
        import socket
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()

            clipboard = QApplication.clipboard()
            clipboard.setText(local_ip)

            # 显示提示
            self.ip_label.setToolTip(I18n.tr('copied'))
            QTimer.singleShot(2000, lambda: self.ip_label.setToolTip(I18n.tr('click_to_copy_ip')))
        except Exception:
            pass
    
    def on_file_receive_start(self, filename: str, file_size: int):
        """开始接收远程文件（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_receive_start", Qt.QueuedConnection,
                                 Q_ARG(str, filename), Q_ARG(int, file_size))
    
    @Slot(str, int)
    def _do_file_receive_start(self, filename: str, file_size: int):
        """实际执行：开始接收远程文件

        Args:
            filename: 文件名（相对路径）
            file_size: 文件大小（字节）
        """
        # 标记文件正在同步，避免循环同步
        file_path = str(self.room_folder / filename)
        self.file_list.mark_syncing(file_path)

        # 清除取消标记，让新接收能添加新进度条
        self._cancelled_transfers.discard(filename)

        # 所有文件都显示进度条（统一逻辑）
        # 如果已有同名进度行（可能是发送进度残留），重置为接收进度行
        if filename in self._transfer_rows:
            transfer_info = self._transfer_rows[filename]
            row = transfer_info['row']
            # 更新操作列为"接收"
            action_item = self.records_table.item(row, 0)
            if action_item:
                action_item.setText("接收")
            # 重置信息列为"接收 文件名"
            from pathlib import Path
            display_name = Path(filename).name
            if len(display_name) > 25:
                display_name = display_name[:22] + "..."
            info_item = self.records_table.item(row, 1)
            if info_item:
                info_item.setText(f"接收 {display_name}")
            # 更新传输信息
            transfer_info['action'] = '接收'
            transfer_info['target_ip'] = ''
        else:
            # 在表格中添加进度行（接收）
            self._add_transfer_progress(filename, 0, 0, "接收")
    
    def on_file_receive_progress(self, filename: str, current: int, total: int):
        """文件接收进度（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_receive_progress", Qt.QueuedConnection,
                                 Q_ARG(str, filename), Q_ARG(int, current), Q_ARG(int, total))
    
    @Slot(str, int, int)
    def _do_file_receive_progress(self, filename: str, current: int, total: int):
        """实际执行：更新文件接收进度"""
        self._update_transfer_progress(filename, current, total)
    
    def on_remote_file_received(self, filename: str):
        """收到远程文件（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_file_received", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    @Slot(str)
    def _do_remote_file_received(self, filename: str):
        """实际执行：收到远程文件"""
        # 取消同步标记
        file_path = str(self.room_folder / filename)
        self.file_list.unmark_syncing(file_path)

        # 更新进度为完成
        self._finish_transfer_progress(filename)

        # 刷新文件列表（不会触发同步信号）
        self.file_list.refresh()

        # 只有当没有其他文件正在同步时，才更新状态为"已连接"
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()

    def on_remote_file_cancelled(self, filename: str):
        """远程文件接收被取消（线程安全）"""
        QMetaObject.invokeMethod(self, "_do_remote_file_cancelled", Qt.QueuedConnection,
                                 Q_ARG(str, filename))

    @Slot(str)
    def _do_remote_file_cancelled(self, filename: str):
        """实际执行：远程文件接收被取消"""
        # 取消同步标记
        file_path = str(self.room_folder / filename)
        self.file_list.unmark_syncing(file_path)

        # 标记进度条为"已取消"并清理占位
        self._cancel_transfer_progress(filename)

        # 刷新文件列表
        self.file_list.refresh()

        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()
    
    def on_remote_file_deleted(self, filename: str):
        """远程文件已删除（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_file_deleted", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    @Slot(str)
    def _do_remote_file_deleted(self, filename: str):
        """实际执行：远程文件已删除"""
        # 刷新文件列表
        self.file_list.refresh()
        
        from pathlib import Path
        self._add_record(Path(filename).name, "删除", "")
        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()
    
    def on_file_send_progress(self, filename: str, current: int, total: int):
        """主机端发送文件进度（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_send_progress", Qt.QueuedConnection,
                                 Q_ARG(str, filename), Q_ARG(int, current), Q_ARG(int, total))
    
    @Slot(str, int, int)
    def _do_file_send_progress(self, filename: str, current: int, total: int):
        """实际执行：更新发送进度

        Args:
            filename: 文件名（相对路径）
            current: 当前已发送的KB数
            total: 总KB数
        """
        # 忽略已取消传输的残留进度信号
        if filename in self._cancelled_transfers:
            return

        # 所有文件都显示进度条（统一逻辑）
        if filename not in self._transfer_rows:
            self._add_transfer_progress(filename, current, total, "发送")
        else:
            # 更新进度
            self._update_transfer_progress(filename, current, total)
    
    def on_file_sent(self, filename: str):
        """主机端发送文件完成（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_sent", Qt.QueuedConnection,
                                 Q_ARG(str, filename))
    
    @Slot(str)
    def _do_file_sent(self, filename: str):
        """实际执行：发送文件完成"""
        # 完成进度条（发送）
        self._finish_transfer_progress(filename, "发送")
        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()

    def on_file_forward_progress(self, target_ip: str, filename: str, current: int, total: int):
        """主机端转发文件进度（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_file_forward_progress", Qt.QueuedConnection,
                                 Q_ARG(str, target_ip), Q_ARG(str, filename), Q_ARG(int, current), Q_ARG(int, total))

    @Slot(str, str, int, int)
    def _do_file_forward_progress(self, target_ip: str, filename: str, current: int, total: int):
        """实际执行：更新转发进度

        Args:
            target_ip: 目标IP地址
            filename: 文件名（相对路径）
            current: 当前已发送的KB数
            total: 总KB数
        """
        # 忽略已取消传输的残留进度信号
        # 使用复合键 client_id:filename 来判断是否已取消
        # 但这里只有 target_ip，所以需要构建复合键（但这可能不准确）
        # 暂时使用 filename 判断
        if filename in self._cancelled_transfers:
            return

        # 所有文件都显示进度条（统一逻辑）
        # 使用复合键来区分不同目标的转发
        transfer_key = f"{target_ip}:{filename}"
        if transfer_key not in self._transfer_rows:
            self._add_transfer_progress(filename, current, total, "发送", target_ip)
        else:
            # 更新进度
            self._update_transfer_progress(filename, current, total, target_ip)

    def on_file_forward_sent(self, target_ip: str, filename: str):
        """主机端转发文件完成（线程安全）"""
        QMetaObject.invokeMethod(self, "_do_file_forward_sent", Qt.QueuedConnection,
                                 Q_ARG(str, target_ip), Q_ARG(str, filename))

    def on_file_forward_cancelled(self, target_ip: str, filename: str):
        """主机端转发文件被取消（线程安全）"""
        QMetaObject.invokeMethod(self, "_do_file_forward_cancelled", Qt.QueuedConnection,
                                 Q_ARG(str, target_ip), Q_ARG(str, filename))

    @Slot(str, str)
    def _do_file_forward_cancelled(self, target_ip: str, filename: str):
        """实际执行：主机端转发文件被取消，清理对应转发进度条（ip:filename 复合键）"""
        self._cancel_transfer_progress(filename, target_ip)

    @Slot(str, str)
    def _do_file_forward_sent(self, target_ip: str, filename: str):
        """实际执行：转发文件完成"""
        self._finish_transfer_progress(filename, "发送", target_ip)
        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()

    def on_remote_file_renamed(self, old_name: str, new_name: str):
        """远程文件已重命名（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_file_renamed", Qt.QueuedConnection,
                                 Q_ARG(str, old_name), Q_ARG(str, new_name))
    
    @Slot(str, str)
    def _do_remote_file_renamed(self, old_name: str, new_name: str):
        """实际执行：远程文件已重命名"""
        # 刷新文件列表
        self.file_list.refresh()
        
        self._add_record(f"{old_name} -> {new_name}", I18n.tr("log_change"), "")
        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()
    
    def on_remote_dir_created(self, dirname: str):
        """远程目录已创建（线程安全）"""
        # 使用 QMetaObject.invokeMethod 确保在主线程执行
        QMetaObject.invokeMethod(self, "_do_remote_dir_created", Qt.QueuedConnection,
                                 Q_ARG(str, dirname))
    
    @Slot(str)
    def _do_remote_dir_created(self, dirname: str):
        """实际执行：远程目录已创建"""
        # 刷新文件列表
        self.file_list.refresh()
        
        from pathlib import Path
        self._add_record(Path(dirname).name, "创建目录", "")
        # 只有当没有其他文件正在同步时，才更新状态为"已连接"（连接端保留延迟显示）
        if not self._transfer_rows:
            if self.is_host:
                self._update_clients_count()
            else:
                self._refresh_client_status_label()
    
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
        
        # 添加新行：普通记录插在活动进度行"钉住区"之上，保证进度行始终位于表格底部
        pinned = self._pinned_count()
        row_count = max(0, self.records_table.rowCount() - pinned)
        self.records_table.insertRow(row_count)
        
        # 操作
        action_item = QTableWidgetItem(action)
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(row_count, 0, action_item)
        
        # 内容
        content_item = QTableWidgetItem(display_text)
        # 完整内容存入 tooltip，供导出时使用
        full_text = f"{content} - {status}" if status else content
        content_item.setToolTip(full_text)
        self.records_table.setItem(row_count, 1, content_item)
        
        # 滚动到底部
        self.records_table.scrollToBottom()
        
        # 只裁剪顶部历史记录，活动进度行"钉住区"始终不被裁剪
        self._trim_history()

    def _export_log(self):
        """导出传输日志到文件"""
        from datetime import datetime
        from PySide6.QtWidgets import QFileDialog

        # 生成默认文件名：日志[当前日期和时间].txt
        now = datetime.now()
        default_name = f"日志[{now.strftime('%Y-%m-%d %H-%M-%S')}].txt"

        # 弹出保存对话框
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            I18n.tr('export_log'),
            default_name,
            "文本文件 (*.txt);;所有文件 (*)"
        )

        if not file_path:
            return  # 用户取消

        try:
            # 收集表格内容
            lines = []
            # 日志文件第一行写入完整串号（名称.版本号.串号）
            lines.append(Config.APP_SERIAL_FULL)
            # 第二行写入导出时间（精确到秒，无标题）
            lines.append(now.strftime('%Y-%m-%d %H:%M:%S'))
            for row in range(self.records_table.rowCount()):
                action_item = self.records_table.item(row, 0)
                info_item = self.records_table.item(row, 1)

                action = action_item.text() if action_item else ""
                # 优先使用 tooltip（完整内容），没有则回退到显示文本
                info = info_item.toolTip() if info_item and info_item.toolTip() else (info_item.text() if info_item else "")

                # 格式化为一行
                lines.append(f"{action}\t{info}")

            # 追加隐藏日志（广播文件等）
            for log_type, message in self._hidden_logs:
                lines.append(f"{log_type}\t{message}")

            # 写入文件
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))

            self._add_record("", "导出", f"日志已导出: {file_path}")

        except Exception as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, I18n.tr('export_log'), f"导出失败: {e}")

    def _add_transfer_progress(self, filename: str, current: int, total: int, action: str = "接收", target_ip: str = ""):
        """添加传输进度行（使用进度条控件）

        Args:
            filename: 文件名（相对路径）
            current: 当前进度（KB）
            total: 总大小（KB）
            action: 操作类型（"发送" / "接收"）
            target_ip: 目标IP（可选，用于转发时显示）
        """
        from pathlib import Path

        # 截断文件名
        display_name = Path(filename).name
        if len(display_name) > 25:
            display_name = display_name[:22] + "..."

        # 构建显示文本（开始状态）
        if target_ip:
            display_text = f"发送至 {target_ip} {display_name}"
        else:
            display_text = f"{action} {display_name}"

        # 添加新行
        row_count = self.records_table.rowCount()
        self.records_table.insertRow(row_count)

        # 操作
        action_item = QTableWidgetItem(action)
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(row_count, 0, action_item)

        # 创建进度条控件
        progress_bar = QProgressBar()
        progress_bar.setRange(0, 100)
        progress_bar.setValue(0)
        progress_bar.setTextVisible(True)  # 显示百分比文本
        progress_bar.setFormat(f"{display_text} - %p%")  # 显示"文件名 - 百分比%"
        progress_bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)  # 文本左对齐
        
        # 设置进度条样式（绿色填充，背景透明）
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

        # 设置进度条到表格的第二列
        self.records_table.setCellWidget(row_count, 1, progress_bar)

        # 调整行高以适应进度条
        self.records_table.setRowHeight(row_count, 25)

        # 构建复合键（如果有目标IP，使用复合键；否则使用文件名）
        transfer_key = f"{target_ip}:{filename}" if target_ip else filename

        # 记录行号和传输信息（用于后续更新）
        self._transfer_rows[transfer_key] = {
            'row': row_count,
            'action': action,
            'target_ip': target_ip,
            'filename': filename
        }

        # 滚动到底部，并裁剪历史区、校正钉住区行号
        self.records_table.scrollToBottom()
        self._trim_history()

    def _update_transfer_progress(self, filename: str, current: int, total: int, target_ip: str = ""):
        """更新传输进度（更新进度条控件）

        Args:
            filename: 文件名（相对路径）
            current: 当前已传输的KB数
            total: 总KB数
            target_ip: 目标IP（可选，用于转发时显示）
        """
        # 构建复合键（如果有目标IP，使用复合键；否则使用文件名）
        transfer_key = f"{target_ip}:{filename}" if target_ip else filename

        # 忽略已取消传输的残留进度信号
        if transfer_key in self._cancelled_transfers:
            return

        if transfer_key in self._transfer_rows:
            transfer_info = self._transfer_rows[transfer_key]
            row = transfer_info['row']
            action = transfer_info['action']

            # 获取进度条控件
            progress_bar = self.records_table.cellWidget(row, 1)
            if progress_bar and isinstance(progress_bar, QProgressBar):
                # 计算进度百分比
                progress_percent = int(current / total * 100) if total > 0 else 0

                # 截断文件名
                from pathlib import Path
                display_name = Path(filename).name
                if len(display_name) > 25:
                    display_name = display_name[:22] + "..."

                # 更新进度条的值
                progress_bar.setValue(progress_percent)

                # 更新进度条的文本格式（显示文件名 + 百分比 + 大小）
                # current 和 total 已经是 KB 单位
                current_mb = current / 1024
                total_mb = total / 1024
                if total_mb >= 1:
                    progress_bar.setFormat(f"{display_name} - {progress_percent}% ({current_mb:.1f}/{total_mb:.1f}M)")
                else:
                    progress_bar.setFormat(f"{display_name} - {progress_percent}% ({current}/{total}K)")
    
    def _finish_transfer_progress(self, filename: str, action: str = "接收", target_ip: str = ""):
        """完成传输进度（移除进度条，显示完成状态）

        Args:
            filename: 文件名（相对路径）
            action: 操作类型（"发送" / "接收"）
            target_ip: 目标IP（可选，用于转发时显示）
        """
        from pathlib import Path
        display_name = Path(filename).name
        if len(display_name) > 25:
            display_name = display_name[:22] + "..."

        # 构建复合键（如果有目标IP，使用复合键；否则使用文件名）
        transfer_key = f"{target_ip}:{filename}" if target_ip else filename

        # 清理取消标记（传输完成，不再需要抑制残留信号）
        self._cancelled_transfers.discard(transfer_key)

        if transfer_key in self._transfer_rows:
            # 有进度行：直接从钉住区移除该行（进度条随行销毁，安全不复用）
            row = self._transfer_rows[transfer_key]['row']
            del self._transfer_rows[transfer_key]
            self.records_table.removeRow(row)
            # 注意：不在此处 reindex。完成记录 insertRow 会再次移动进度行，
            # 须由末尾 _trim_history 里的 reindex 在 insert 完成后统一校正。此处过早校正会被 insert 破坏。
        # else: 无进度行（文件传输很快），直接新增完成记录，无需移除

        # 构建完成文本
        if target_ip:
            display_text = f"发送至 {target_ip} {display_name} - 完成"
            full_text = f"发送至 {target_ip} {filename} - 完成"
        else:
            display_text = f"{display_name} - 完成"
            full_text = f"{filename} - 完成"

        # 在钉住区之上插入完成记录，使其进入上方历史区流转
        pinned = self._pinned_count()
        insert_row = max(0, self.records_table.rowCount() - pinned)
        self.records_table.insertRow(insert_row)

        # 操作列
        action_item = QTableWidgetItem(action)
        action_item.setTextAlignment(Qt.AlignCenter)
        self.records_table.setItem(insert_row, 0, action_item)

        # 信息列（绿色完成状态）
        status_item = QTableWidgetItem(display_text)
        status_item.setToolTip(full_text)  # 完整路径存入 tooltip，供导出使用
        status_item.setForeground(QColor("#51cf66"))
        self.records_table.setItem(insert_row, 1, status_item)

        # 滚动到底部
        self.records_table.scrollToBottom()

        # 只裁剪顶部历史记录，钉住区永不裁剪
        self._trim_history()

    def _pinned_count(self):
        """返回当前钉在表格底部的活动进度行数量。

        所有 _transfer_rows 中的条目都是活动进度行（完成/取消即 del），
        恒位于表格底部，故以其条目数作为钉住区行数。
        """
        return len(self._transfer_rows)

    def _reindex_block(self):
        """将 _transfer_rows 的行号校正为与底部"钉住区"精确对齐。

        不移动任何控件，仅更新字典中的行号：按相对顺序把活动进度行映射到
        表格最后 len(_transfer_rows) 行，配合插入位置控制维持
        "活动进度行恒在底部、控件永不复用"的安全不变量。
        """
        ordered = sorted((info['row'], key) for key, info in self._transfer_rows.items())
        n = len(ordered)
        base = self.records_table.rowCount() - n
        for i, (_, key) in enumerate(ordered):
            self._transfer_rows[key]['row'] = base + i

    def _trim_history(self, cap: int = 100):
        """只裁剪顶部"历史记录"区，活动进度行钉住区永不裁剪。

        removeRow(0) 会让整表行号下移，因此裁剪后统一 _reindex_block 校正。
        """
        while self.records_table.rowCount() - self._pinned_count() > cap:
            # 活动进度行恒在底部，此处行号0必为历史记录，不会误删钉住行
            self.records_table.removeRow(0)
        self._reindex_block()

    @Slot(str, str)
    def add_log(self, log_type: str, message: str):
        """添加日志（兼容旧代码）"""
        # 广播文件日志不在表格中显示，但会保留在隐藏日志中供导出
        if message.startswith("广播文件:"):
            self._hidden_logs.append((log_type, message))
            return
        self._add_record(message, log_type, "")
    
    @Slot(str)
    def on_cancel_transfer(self, rel_path: str):
        """取消文件传输（删除/重命名前调用，避免 Windows 文件锁）"""
        self.transfer_queue.cancel_tasks_by_filename(rel_path)
        # 清理发送进度条占位，避免再次发送同名文件时复用旧的进度条
        self._cancel_transfer_progress(rel_path)

    def _cancel_transfer_progress(self, filename: str, target_ip: str = ""):
        """取消/失败传输进度条，使其进入上方历史区流转（不再钉在底部）

        Args:
            filename: 文件名（相对路径）
            target_ip: 目标IP（可选，用于转发时显示）
        """
        from pathlib import Path

        # 构建复合键（如果有目标IP，使用复合键；否则使用文件名）
        transfer_key = f"{target_ip}:{filename}" if target_ip else filename

        if transfer_key in self._transfer_rows:
            transfer_info = self._transfer_rows[transfer_key]
            row = transfer_info['row']
            action = transfer_info.get('action', '接收')

            display_name = Path(filename).name
            if len(display_name) > 25:
                display_name = display_name[:22] + "..."

            # 移除钉住行（进度条随行销毁，不复用），使其离开底部钉住区
            del self._transfer_rows[transfer_key]
            self.records_table.removeRow(row)
            # 不在此处 reindex：由末尾 _trim_history 在插入"已取消"记录后统一校正，
            # 避免过早校正被后续 insert 破坏。

            # 在钉住区之上插入"已取消"记录，使其进入上方历史区流转
            pinned = self._pinned_count()
            insert_row = max(0, self.records_table.rowCount() - pinned)
            self.records_table.insertRow(insert_row)

            action_item = QTableWidgetItem(action)
            action_item.setTextAlignment(Qt.AlignCenter)
            self.records_table.setItem(insert_row, 0, action_item)

            # 信息列为橙色取消状态
            status_item = QTableWidgetItem(f"{display_name} - 已取消")
            status_item.setToolTip(f"{filename} - 已取消")  # 完整路径存入 tooltip，供导出使用
            status_item.setForeground(QColor("#ff922b"))
            self.records_table.setItem(insert_row, 1, status_item)

            # 滚动到底部，并只裁剪顶部历史区（钉住区仍不受影响）
            self.records_table.scrollToBottom()
            self._trim_history()

        # 标记为已取消，过滤残留进度信号（同时登记 filename 与复合键，兼容历史遗留判断）
        self._cancelled_transfers.add(filename)
        self._cancelled_transfers.add(transfer_key)
    
    def on_file_added(self, file_path: str):
        """文件添加事件（本地操作）"""
        from pathlib import Path

        file_name = Path(file_path).name
        self._add_record(file_name, "添加", "")

        # 取消该文件的所有传输任务（支持复合键 client_id:filename）
        rel_path = os.path.relpath(file_path, self.room_folder).replace('\\', '/')
        self.transfer_queue.cancel_tasks_by_filename(rel_path)
        # 清除取消标记，让新传输能添加新进度条
        self._cancelled_transfers.discard(rel_path)
        self._transfer_rows.pop(rel_path, None)

        # 定义同步函数
        def sync_file(stop_event: threading.Event):
            try:
                # 检查是否需要停止
                if stop_event.is_set():
                    return
                
                # 根据设计文档的同步逻辑：
                # 主机端：直接广播给所有连接端
                # 连接端：发送给主机端，主机端转发给其他连接端
                if self.is_host and self.server:
                    self.server.broadcast_file(file_path, stop_event)
                elif self.client:
                    self.client.send_file(file_path, stop_event)
            except Exception as e:
                self.add_log("错误", f"同步文件失败: {e}")
        
        # 将任务加入传输队列
        self.transfer_queue.add_task('file', sync_file, rel_path)
    
    def on_file_deleted(self, file_path: str):
        """文件删除事件（本地操作）"""
        from pathlib import Path
        
        file_name = Path(file_path).name
        self._add_record(file_name, "删除", "")
        
        # 取消该文件的所有传输任务（支持复合键 client_id:filename）
        rel_path = os.path.relpath(file_path, self.room_folder).replace('\\', '/')
        self.transfer_queue.cancel_tasks_by_filename(rel_path)

        # 定义同步函数
        def sync_delete(stop_event: threading.Event):
            try:
                # 检查是否需要停止
                if stop_event.is_set():
                    return
                
                if self.is_host and self.server:
                    self.server.broadcast_delete(file_path)
                elif self.client:
                    self.client.send_delete(file_path)
            except Exception as e:
                self.add_log("错误", f"同步删除失败: {e}")
        
        # 将任务加入传输队列
        self.transfer_queue.add_task('delete', sync_delete, rel_path)
    
    def on_file_renamed(self, old_path: str, new_path: str):
        """文件重命名事件（本地操作）"""
        from pathlib import Path
        
        old_name = Path(old_path).name
        new_name = Path(new_path).name
        self.add_log(I18n.tr("log_change"), f"{old_name} -> {new_name}")
        
        # 取消旧文件的传输（如果正在传输）
        old_rel_path = os.path.relpath(old_path, self.room_folder).replace('\\', '/')
        self.transfer_queue.cancel_tasks_by_filename(old_rel_path)

        # 定义同步函数
        def sync_rename(stop_event: threading.Event):
            try:
                # 检查是否需要停止
                if stop_event.is_set():
                    return
                
                if self.is_host and self.server:
                    self.server.broadcast_rename(old_path, new_path)
                elif self.client:
                    self.client.send_rename(old_path, new_path)
            except Exception as e:
                self.add_log("错误", f"同步变更失败: {e}")
        
        # 将任务加入传输队列
        new_rel_path = os.path.relpath(new_path, self.room_folder).replace('\\', '/')
        self.transfer_queue.add_task('rename', sync_rename, new_rel_path)

    def on_dir_created(self, dir_path: str):
        """目录创建事件（本地操作）"""
        from pathlib import Path

        dir_name = Path(dir_path).name
        self._add_record(dir_name, "创建目录", "")

        # 定义同步函数
        def sync_dir_create(stop_event: threading.Event):
            try:
                if stop_event.is_set():
                    return

                if self.is_host and self.server:
                    self.server.broadcast_dir_create(dir_path)
                elif self.client:
                    self.client.send_dir_create(dir_path)
            except Exception as e:
                self.add_log("错误", f"同步创建目录失败: {e}")

        # 将任务加入传输队列
        rel_path = os.path.relpath(dir_path, self.room_folder).replace('\\', '/')
        self.transfer_queue.add_task('dir_create', sync_dir_create, rel_path)

    def on_disconnect(self):
        """断开连接"""
        # 创建自定义消息框
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(I18n.tr('confirm_leave'))
        msg_box.setText(I18n.tr('confirm_leave_msg'))
        msg_box.setIcon(QMessageBox.Question)
        
        # 添加自定义按钮
        yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
        no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)
        
        # 应用全局按钮样式
        yes_btn.setStyleSheet(BUTTON_STYLES['danger'])
        no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        # 统一按钮宽度，与文件替换的“确定/取消”按钮保持一致
        yes_btn.setFixedWidth(80)
        no_btn.setFixedWidth(80)
        
        msg_box.setDefaultButton(no_btn)
        msg_box.exec()

        if msg_box.clickedButton() == yes_btn:
            self._close_confirmed = True
            self.close()
    
    def _show_about(self):
        """显示关于对话框"""
        dialog = AboutDialog(self)
        dialog.exec()

    def _on_clean_cache_changed(self, checked: bool):
        """清理缓存开关状态改变"""
        # 保存状态到配置
        UserConfig.set_clean_cache_enabled(checked)

    def closeEvent(self, event):
        """窗口关闭事件"""
        # 未确认时弹出确认弹窗（点击叉号或外部触发关闭时）
        if not self._close_confirmed:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle(I18n.tr('confirm_leave'))
            msg_box.setText(I18n.tr('confirm_leave_msg'))
            msg_box.setIcon(QMessageBox.Question)

            yes_btn = msg_box.addButton(I18n.tr('yes'), QMessageBox.YesRole)
            no_btn = msg_box.addButton(I18n.tr('no'), QMessageBox.NoRole)

            yes_btn.setStyleSheet(BUTTON_STYLES['danger'])
            no_btn.setStyleSheet(BUTTON_STYLES['secondary'])
            # 统一按钮宽度（与“是否退出房间”的主对话框一致）
            yes_btn.setFixedWidth(80)
            no_btn.setFixedWidth(80)

            msg_box.setDefaultButton(no_btn)
            msg_box.exec()

            if msg_box.clickedButton() != yes_btn:
                event.ignore()
                return

            self._close_confirmed = True

        # 清理传输队列
        self.transfer_queue.clear()
        # 清理局域网剪切板：远程文件拉取队列、目录服务、可用远程文件状态
        self.p2p_queue.clear()
        self._remote_files = None
        self._stop_provider()
        self._remove_temp_self_images()
        
        # 清空整个预览文件夹
        try:
            preview_folder = Config.get_preview_folder()
            if preview_folder.exists():
                # 使用 safe_rmtree 处理 Windows 只读文件导致的权限问题
                from sync.file_manager import safe_rmtree
                safe_rmtree(preview_folder)
        except Exception:
            pass  # 清理失败不影响关闭
        
        # 停止网络服务
        if self.server:
            self.server.stop()
        if self.client:
            self.client.disconnect()
        if self.responder:
            self.responder.stop()

        # 关闭顶部快捷放置区：停轮询定时器、卸载全局钩子、关闭放置条，避免残留
        if self._drag_loop is not None:
            self._drag_loop.stop()
            self._drag_loop = None
        if self._drag_detector is not None:
            self._drag_detector.finish()
            self._drag_detector = None
        if self._drop_zone is not None:
            self._drop_zone.close()
            self._drop_zone = None

        # 清理缓存（如果开关开启）
        if self.clean_cache_switch.isChecked():
            try:
                # 清理当前房间号的缓存文件夹（异步清理）
                if self.room_folder.exists():
                    # 使用清理队列异步清理，避免文件锁问题
                    get_clean_queue().add_clean_task(self.room_folder)
            except Exception:
                pass  # 清理失败不影响关闭

        self.closed.emit()
        event.accept()
