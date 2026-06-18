# -*- coding: utf-8 -*-
"""
LANSyncBox 加入房间对话框 - 自动搜索并连接（紧凑版）
"""

import os
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QMessageBox, QGroupBox, QProgressBar
)
from PyQt5.QtCore import Qt, QTimer

from config import STYLESHEET, COLORS, DEFAULT_PORT
from ui.widgets import AnimatedButton
from network.discovery import HostDiscovery


class JoinRoomDialog(QDialog):
    """加入房间对话框 - 输入房间号自动搜索并连接（紧凑）"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("加入连接")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumSize(320, 260)
        self.setStyleSheet(STYLESHEET)
        
        # 房间信息
        self.room_code = ""
        self.password = ""
        self.sync_folder = ""
        self.host_address = ""
        
        # 主机发现
        self.discovery = HostDiscovery(self)
        self.discovery.host_found.connect(self._on_host_found)
        self.discovery.discovery_finished.connect(self._on_discovery_finished)
        self.discovery.error_occurred.connect(self._show_error)
        
        # 自动连接状态
        self._auto_connecting = False
        self._found_host = None
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # 房间号输入组
        room_group = QGroupBox("房间信息")
        room_layout = QVBoxLayout(room_group)
        room_layout.setSpacing(6)
        
        # 房间号
        room_code_layout = QHBoxLayout()
        room_code_label = QLabel("房间号：")
        room_code_label.setMinimumWidth(60)
        room_code_layout.addWidget(room_code_label)
        
        self.room_code_input = QLineEdit()
        self.room_code_input.setPlaceholderText("输入6位数字")
        self.room_code_input.setMaxLength(6)
        self.room_code_input.textChanged.connect(self._on_room_code_changed)
        room_code_layout.addWidget(self.room_code_input)
        
        room_layout.addLayout(room_code_layout)
        
        # 密码输入
        password_layout = QHBoxLayout()
        password_label = QLabel("密码：")
        password_label.setMinimumWidth(60)
        password_layout.addWidget(password_label)
        
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText("可选，若主机设置了密码")
        self.password_input.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(self.password_input)
        
        room_layout.addLayout(password_layout)
        
        # 状态显示
        self.status_label = QLabel("输入房间号后自动搜索")
        self.status_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10px;")
        self.status_label.setWordWrap(True)
        room_layout.addWidget(self.status_label)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.hide()
        room_layout.addWidget(self.progress_bar)
        
        layout.addWidget(room_group)
        
        # 同步文件夹选择组
        folder_group = QGroupBox("同步文件夹")
        folder_layout = QVBoxLayout(folder_group)
        
        folder_path_layout = QHBoxLayout()
        self.folder_path_input = QLineEdit()
        self.folder_path_input.setPlaceholderText("选择文件夹")
        self.folder_path_input.setReadOnly(True)
        folder_path_layout.addWidget(self.folder_path_input)
        
        self.folder_btn = QPushButton("选择")
        self.folder_btn.setMinimumWidth(60)
        self.folder_btn.clicked.connect(self._select_folder)
        folder_path_layout.addWidget(self.folder_btn)
        
        folder_layout.addLayout(folder_path_layout)
        
        layout.addWidget(folder_group)
        
        layout.addStretch()
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setMinimumSize(80, 32)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        btn_layout.addSpacing(10)
        
        self.join_btn = AnimatedButton("连接")
        self.join_btn.setObjectName("successBtn")
        self.join_btn.setMinimumSize(80, 32)
        self.join_btn.clicked.connect(self._on_join)
        btn_layout.addWidget(self.join_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
    
    def _on_room_code_changed(self, text):
        """房间号输入变化时自动开始搜索"""
        if len(text) == 6 and text.isdigit() and not self._auto_connecting:
            self._start_auto_search(text)
    
    def _start_auto_search(self, room_code):
        """自动开始搜索"""
        self._auto_connecting = True
        self._found_host = None
        self.room_code_input.setEnabled(False)
        self.status_label.setText("正在搜索...")
        self.status_label.setStyleSheet(f"color: {COLORS['primary']}; font-size: 10px;")
        self.progress_bar.show()
        
        # 模拟进度
        self._progress_value = 0
        self._progress_timer = QTimer(self)
        self._progress_timer.timeout.connect(self._update_progress)
        self._progress_timer.start(50)
        
        # 启动发现
        self.discovery.start_discovery(room_code, timeout=3)
    
    def _update_progress(self):
        """更新进度条"""
        self._progress_value += 3
        if self._progress_value > 100:
            self._progress_value = 100
        self.progress_bar.setValue(self._progress_value)
    
    def _on_host_found(self, ip, room_code):
        """发现主机 - 自动选择第一个匹配的主机"""
        if self._found_host is None:
            self._found_host = ip
            self.host_address = ip
            self.room_code = room_code
            
            # 立即停止搜索并显示找到
            self.discovery.stop_discovery()
            self._progress_timer.stop()
            self.progress_bar.hide()
            
            self.status_label.setText(f"已找到: {ip}")
            self.status_label.setStyleSheet(f"color: {COLORS['success']}; font-size: 10px;")
            self.room_code_input.setEnabled(True)
            self._auto_connecting = False
    
    def _on_discovery_finished(self, hosts):
        """发现完成"""
        self._progress_timer.stop()
        self.progress_bar.hide()
        self.room_code_input.setEnabled(True)
        self._auto_connecting = False
        
        if self._found_host:
            return
        
        if hosts:
            self.status_label.setText("房间号不匹配")
            self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 10px;")
        else:
            self.status_label.setText("未找到主机")
            self.status_label.setStyleSheet(f"color: {COLORS['text_secondary']}; font-size: 10px;")
    
    def _select_folder(self):
        """选择同步文件夹"""
        folder = QFileDialog.getExistingDirectory(
            self, "选择同步文件夹",
            os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly
        )
        
        if folder:
            self.folder_path_input.setText(folder)
    
    def _show_error(self, message):
        """显示错误"""
        self._progress_timer.stop()
        self.progress_bar.hide()
        self.room_code_input.setEnabled(True)
        self._auto_connecting = False
        self.status_label.setText(f"错误: {message}")
        self.status_label.setStyleSheet(f"color: {COLORS['danger']}; font-size: 10px;")
    
    def _on_join(self):
        """加入房间"""
        room_code = self.room_code_input.text()
        password = self.password_input.text()
        sync_folder = self.folder_path_input.text()
        
        # 验证房间号
        if len(room_code) != 6 or not room_code.isdigit():
            QMessageBox.warning(self, "提示", "请输入6位数字房间号")
            return
        
        # 验证是否找到主机
        if not self.host_address:
            QMessageBox.warning(self, "提示", "未找到主机")
            return
        
        # 验证同步文件夹
        if not sync_folder:
            QMessageBox.warning(self, "提示", "请选择同步文件夹")
            return
        
        if not os.path.isdir(sync_folder):
            QMessageBox.warning(self, "提示", "文件夹不存在")
            return
        
        self.room_code = room_code
        self.sync_folder = sync_folder
        self.password = password
        
        self.accept()