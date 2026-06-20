"""
创建房间对话框
"""
import random
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, 
    QLineEdit, QPushButton, QFrame, QMessageBox, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette, QFont

from i18n import I18n
from config import Config
from ui.widgets import AnimatedButton, BUTTON_STYLES


class RoomCodeDisplay(QWidget):
    """房间号显示组件 - 6个格子显示6个数字"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.digit_labels = []
        self._init_ui()
    
    def _init_ui(self):
        """初始化界面"""
        layout = QHBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)
        
        # 创建6个数字格子
        for i in range(6):
            label = QLabel("-")
            label.setAlignment(Qt.AlignCenter)
            label.setMinimumSize(40, 50)
            label.setMaximumSize(50, 60)
            
            # 使用系统颜色适配深色/浅色模式
            label.setStyleSheet("""
                QLabel {
                    font-size: 28px;
                    font-weight: bold;
                    background-color: palette(window);
                    border: 2px solid palette(mid);
                    border-radius: 6px;
                    color: palette(text);
                }
            """)
            
            self.digit_labels.append(label)
            layout.addWidget(label)
        
        # 设置字体
        font = QFont()
        font.setPointSize(20)
        font.setBold(True)
        for label in self.digit_labels:
            label.setFont(font)
    
    def set_room_code(self, code: str):
        """设置房间号"""
        # 确保是6位数字
        code = code.zfill(6)
        for i, digit in enumerate(code[:6]):
            self.digit_labels[i].setText(digit)
    
    def get_room_code(self) -> str:
        """获取房间号"""
        return "".join(label.text() for label in self.digit_labels)


class CreateRoomDialog(QDialog):
    """创建房间对话框"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.room_code = ""
        self.password = ""
        self.init_ui()
    
    def init_ui(self):
        """初始化界面"""
        self.setWindowTitle(I18n.tr('create_room_title'))
        self.setModal(True)
        self.setFixedWidth(400)
        
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 房间号显示
        room_code_layout = QVBoxLayout()
        room_code_label = QLabel(I18n.tr('room_code'))
        room_code_layout.addWidget(room_code_label)
        
        # 房间号显示组件（6个格子）
        self.room_code_display = RoomCodeDisplay()
        room_code_layout.addWidget(self.room_code_display)
        
        # 自动生成随机房间号
        self.generate_room_code()
        
        # 重新生成按钮
        regenerate_btn_layout = QHBoxLayout()
        regenerate_btn_layout.addStretch()
        self.regenerate_btn = AnimatedButton(I18n.tr('regenerate_room_code'))
        self.regenerate_btn.setFixedWidth(120)
        self.regenerate_btn.clicked.connect(self.generate_room_code)
        self.regenerate_btn.setStyleSheet(BUTTON_STYLES['outline'])
        regenerate_btn_layout.addWidget(self.regenerate_btn)
        room_code_layout.addLayout(regenerate_btn_layout)
        
        layout.addLayout(room_code_layout)
        
        # 分隔线
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line)
        
        # 密码输入
        password_layout = QVBoxLayout()
        password_label = QLabel(I18n.tr('password'))
        password_layout.addWidget(password_label)
        
        self.password_edit = QLineEdit()
        self.password_edit.setPlaceholderText(I18n.tr('password_hint'))
        self.password_edit.setEchoMode(QLineEdit.Password)
        password_layout.addWidget(self.password_edit)
        
        layout.addLayout(password_layout)
        
        # 分隔线
        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        line2.setFrameShadow(QFrame.Sunken)
        layout.addWidget(line2)
        
        # 同步文件夹信息
        folder_layout = QVBoxLayout()
        folder_label = QLabel(I18n.tr('sync_folder'))
        folder_layout.addWidget(folder_label)
        
        self.folder_path_label = QLabel(str(Config.get_sync_folder()))
        self.folder_path_label.setWordWrap(True)
        folder_layout.addWidget(self.folder_path_label)
        
        layout.addLayout(folder_layout)
        
        # 弹性空间
        layout.addStretch()
        
        # 按钮
        button_layout = QHBoxLayout()
        button_layout.setSpacing(10)
        
        self.create_btn = AnimatedButton(I18n.tr('create'))
        self.create_btn.setFixedWidth(100)
        self.create_btn.clicked.connect(self.on_create)
        self.create_btn.setDefault(True)
        self.create_btn.setStyleSheet(BUTTON_STYLES['primary'])
        
        self.cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self.cancel_btn.setFixedWidth(100)
        self.cancel_btn.clicked.connect(self.reject)
        self.cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        
        button_layout.addStretch()
        button_layout.addWidget(self.create_btn)
        button_layout.addWidget(self.cancel_btn)
        
        layout.addLayout(button_layout)
    
    def generate_room_code(self):
        """生成随机房间号"""
        room_code = str(random.randint(Config.ROOM_CODE_MIN, Config.ROOM_CODE_MAX))
        self.room_code_display.set_room_code(room_code)
    
    def on_create(self):
        """创建房间"""
        # 保存信息
        self.room_code = self.room_code_display.get_room_code()
        self.password = self.password_edit.text()
        
        # 接受对话框
        self.accept()
    
    def get_room_code(self) -> str:
        """获取房间号"""
        return self.room_code
    
    def get_password(self) -> str:
        """获取密码"""
        return self.password
