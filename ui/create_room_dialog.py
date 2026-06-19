# -*- coding: utf-8 -*-
"""
LANSyncBox 创建房间对话框（紧凑版）
"""

import os
import random
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QCheckBox, QFileDialog, QMessageBox, QGroupBox,
    QWidget
)
from PyQt5.QtCore import Qt

from config import STYLESHEET, COLORS, DEFAULT_PORT, ROOM_CODE_LENGTH
from ui.widgets import AnimatedButton
from i18n import I18n

# 延迟导入：RoomManager 在需要时才导入
# from room.room_manager import RoomManager


def _generate_room_code() -> str:
    """生成随机房间号（本地函数，避免导入 RoomManager）"""
    return ''.join([str(random.randint(0, 9)) for _ in range(ROOM_CODE_LENGTH)])


class CreateRoomDialog(QDialog):
    """创建房间对话框（紧凑）"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.t('create_title'))
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setMinimumSize(350, 320)
        self.setStyleSheet(STYLESHEET)
        
        # 房间信息
        self.room_code = ""
        self.password = ""
        self.sync_folder = ""
        self.allow_peer_sync = False
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout()
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # 房间号输入组
        room_group = QGroupBox(I18n.t('create_room_label').rstrip('：'))
        room_layout = QVBoxLayout(room_group)
        room_layout.setSpacing(8)
        
        # 房间号
        room_code_layout = QHBoxLayout()
        room_code_label = QLabel(I18n.t('create_room_label'))
        room_code_label.setMinimumWidth(60)
        room_code_layout.addWidget(room_code_label)
        
        self.room_code_input = QLineEdit()
        self.room_code_input.setPlaceholderText(I18n.t('create_room_placeholder'))
        self.room_code_input.setMaxLength(6)
        self.room_code_input.setText(_generate_room_code())
        room_code_layout.addWidget(self.room_code_input)
        
        # 检测按钮
        self.check_btn = QPushButton(I18n.t('common_check') if hasattr(I18n, 't') and 'common_check' in I18n.get_all_keys() else "检测")
        self.check_btn.setMinimumWidth(50)
        self.check_btn.clicked.connect(self._check_room_code)
        room_code_layout.addWidget(self.check_btn)
        
        room_layout.addLayout(room_code_layout)
        
        # 密码设置
        password_layout = QHBoxLayout()
        password_label = QLabel(I18n.t('create_password_label'))
        password_label.setMinimumWidth(60)
        password_layout.addWidget(password_label)
        
        self.password_input = QLineEdit()
        self.password_input.setPlaceholderText(I18n.t('create_password_placeholder'))
        self.password_input.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(self.password_input)
        
        room_layout.addLayout(password_layout)
        
        layout.addWidget(room_group)
        
        # 同步文件夹选择组
        folder_group = QGroupBox(I18n.t('create_folder_label').rstrip('：'))
        folder_layout = QVBoxLayout(folder_group)
        folder_layout.setSpacing(8)
        
        folder_path_layout = QHBoxLayout()
        self.folder_path_input = QLineEdit()
        self.folder_path_input.setPlaceholderText(I18n.t('common_select_folder'))
        self.folder_path_input.setReadOnly(True)
        folder_path_layout.addWidget(self.folder_path_input)
        
        self.folder_btn = QPushButton(I18n.t('create_folder_select'))
        self.folder_btn.setMinimumWidth(60)
        self.folder_btn.clicked.connect(self._select_folder)
        folder_path_layout.addWidget(self.folder_btn)
        
        folder_layout.addLayout(folder_path_layout)
        
        layout.addWidget(folder_group)
        
        # 选项组
        options_group = QGroupBox(I18n.t('common_options') if hasattr(I18n, 't') and 'common_options' in I18n.get_all_keys() else "选项")
        options_layout = QVBoxLayout(options_group)
        
        self.peer_sync_checkbox = QCheckBox(I18n.t('create_peer_sync'))
        options_layout.addWidget(self.peer_sync_checkbox)
        
        layout.addWidget(options_group)
        
        layout.addStretch()
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        self.cancel_btn = QPushButton(I18n.t('create_cancel'))
        self.cancel_btn.setObjectName("dangerBtn")
        self.cancel_btn.setMinimumSize(80, 32)
        self.cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self.cancel_btn)
        
        btn_layout.addSpacing(10)
        
        self.create_btn = AnimatedButton(I18n.t('create_btn'))
        self.create_btn.setObjectName("successBtn")
        self.create_btn.setMinimumSize(80, 32)
        self.create_btn.clicked.connect(self._on_create)
        btn_layout.addWidget(self.create_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
    
    def _check_room_code(self):
        """检测房间号是否已被占用"""
        # 延迟导入 RoomManager
        from room.room_manager import RoomManager
        
        room_code = self.room_code_input.text()
        
        if len(room_code) != 6 or not room_code.isdigit():
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_room_invalid'))
            return
        
        room_manager = RoomManager()
        if room_manager.is_room_exists(room_code):
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_room_exists'))
            self.room_code_input.setText(_generate_room_code())
        else:
            QMessageBox.information(self, I18n.t('common_info'), f"房间号 {room_code} 可用")
    
    def _select_folder(self):
        """选择同步文件夹"""
        folder = QFileDialog.getExistingDirectory(
            self, I18n.t('common_select_folder'),
            os.path.expanduser("~"),
            QFileDialog.ShowDirsOnly
        )
        
        if folder:
            self.folder_path_input.setText(folder)
    
    def _on_create(self):
        """创建房间"""
        # 延迟导入 RoomManager
        from room.room_manager import RoomManager
        
        room_code = self.room_code_input.text()
        password = self.password_input.text()
        sync_folder = self.folder_path_input.text()
        allow_peer_sync = self.peer_sync_checkbox.isChecked()
        
        # 验证房间号
        if len(room_code) != 6 or not room_code.isdigit():
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_room_invalid'))
            return
        
        # 验证同步文件夹
        if not sync_folder:
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_folder_required'))
            return
        
        if not os.path.isdir(sync_folder):
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_folder_required'))
            return
        
        # 检测房间号是否可用
        room_manager = RoomManager()
        if room_manager.is_room_exists(room_code):
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_room_exists'))
            return
        
        # 创建房间（使用用户输入的房间号）
        created_code = room_manager.create_room(
            sync_folder, password, allow_peer_sync, room_code=room_code
        )
        if created_code:
            self.room_code = created_code
            self.password = password
            self.sync_folder = sync_folder
            self.allow_peer_sync = allow_peer_sync
            self.accept()
        else:
            QMessageBox.warning(self, I18n.t('common_info'), I18n.t('create_failed') if hasattr(I18n, 't') and 'create_failed' in I18n.get_all_keys() else "创建房间失败，请重试")