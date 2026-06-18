# -*- coding: utf-8 -*-
"""
LANSyncBox 关于对话框 - 严格按模板实现
"""

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QApplication
)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices

from config import STYLESHEET, COLORS, APP_NAME, APP_VERSION, APP_AUTHOR, APP_EMAIL, APP_REPO


class AboutDialog(QDialog):
    """关于弹窗"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("关于")
        # 移除右上角的问号按钮
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setFixedSize(400, 320)
        self.setStyleSheet(STYLESHEET)
        self._init_ui()
    
    def _init_ui(self):
        """初始化UI"""
        layout = QVBoxLayout()
        layout.setSpacing(12)
        layout.setContentsMargins(25, 25, 25, 25)
        
        # 标题
        title_label = QLabel(APP_NAME)
        title_label.setStyleSheet("""
            QLabel {
                font-size: 22px;
                font-weight: bold;
                color: #339af0;
            }
        """)
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)
        
        # 版本信息
        version_label = QLabel(f"版本：{APP_VERSION}")
        version_label.setStyleSheet("font-size: 11px; color: #495057;")
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label)
        
        # 描述
        desc_label = QLabel("局域网文件同步工具")
        desc_label.setStyleSheet("font-size: 10px; color: #868e96;")
        desc_label.setAlignment(Qt.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        layout.addSpacing(8)
        
        # 作者信息
        author_label = QLabel(f"作者：{APP_AUTHOR}")
        author_label.setStyleSheet("font-size: 10px; color: #495057;")
        author_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(author_label)
        
        # GitHub链接（可点击）
        github_label = QLabel(f"GitHub: {APP_REPO}")
        github_label.setStyleSheet("""
            QLabel {
                font-size: 10px;
                color: #339af0;
            }
            QLabel:hover {
                color: #228be6;
            }
        """)
        github_label.setAlignment(Qt.AlignCenter)
        github_label.setCursor(Qt.PointingHandCursor)
        github_label.mousePressEvent = lambda event: self._open_github()
        layout.addWidget(github_label)
        
        # 邮箱（可点击复制）
        email_label = QLabel(f"Email: {APP_EMAIL}")
        email_label.setStyleSheet("""
            QLabel {
                font-size: 10px;
                color: #495057;
            }
            QLabel:hover {
                color: #339af0;
            }
        """)
        email_label.setAlignment(Qt.AlignCenter)
        email_label.setCursor(Qt.PointingHandCursor)
        email_label.mousePressEvent = lambda event: self._copy_email()
        layout.addWidget(email_label)
        
        layout.addSpacing(10)
        
        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        
        # 检查更新按钮
        check_update_btn = QPushButton("检查更新")
        check_update_btn.setMinimumWidth(120)
        check_update_btn.setFixedHeight(36)
        check_update_btn.setStyleSheet("""
            QPushButton {
                background-color: #339af0;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 0 10px;
            }
            QPushButton:hover {
                background-color: #228be6;
            }
        """)
        check_update_btn.clicked.connect(self._check_update)
        btn_layout.addWidget(check_update_btn)
        
        btn_layout.addSpacing(10)
        
        # 关闭按钮
        close_btn = QPushButton("关闭")
        close_btn.setFixedSize(100, 36)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)
        
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
        
        self.setLayout(layout)
    
    def _open_github(self):
        """打开GitHub链接"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{APP_REPO}"))
    
    def _copy_email(self):
        """复制邮箱到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(APP_EMAIL)
    
    def _check_update(self):
        """检查更新"""
        import urllib.request
        import urllib.error
        import json
        import re
        
        try:
            # 获取GitHub仓库的tags列表
            url = f"https://api.github.com/repos/{APP_REPO}/tags"
            req = urllib.request.Request(url)
            req.add_header('User-Agent', APP_NAME)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
            
            if not data:
                QMessageBox.information(self, "检查更新", "未找到版本标签")
                return
            
            # 获取最新tag
            latest_tag = data[0]['name']
            
            # 解析当前版本号
            current_version_match = re.search(r'R(\d+)', APP_VERSION)
            if not current_version_match:
                QMessageBox.warning(self, "检查更新", "无法解析当前版本号")
                return
            current_version = int(current_version_match.group(1))
            
            # 解析远程版本号
            latest_version_match = re.search(r'R(\d+)', latest_tag)
            if not latest_version_match:
                QMessageBox.warning(self, "检查更新", "无法解析远程版本号")
                return
            latest_version = int(latest_version_match.group(1))
            
            # 比较版本号
            if latest_version > current_version:
                # 发现新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("检查更新")
                msg_box.setText(f"发现新版本 {latest_tag}！\n是否前往GitHub下载？")
                msg_box.setIcon(QMessageBox.NoIcon)
                msg_box.setStyleSheet("""
                    QMessageBox {
                        font-size: 11px;
                    }
                    QMessageBox QLabel {
                        color: #495057;
                        font-size: 11px;
                        padding: 10px;
                    }
                """)
                
                # 自定义按钮
                yes_btn = msg_box.addButton("是", QMessageBox.YesRole)
                no_btn = msg_box.addButton("否", QMessageBox.NoRole)
                
                # 绿色"是"按钮
                yes_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #51cf66;
                        color: white;
                        border: none;
                        border-radius: 6px;
                        padding: 8px 24px;
                        min-width: 80px;
                        font-size: 11px;
                    }
                    QPushButton:hover {
                        background-color: #40c057;
                    }
                """)
                
                # 红色"否"按钮
                no_btn.setStyleSheet("""
                    QPushButton {
                        background-color: #ff6b6b;
                        color: white;
                        border: none;
                        border-radius: 6px;
                        padding: 8px 24px;
                        min-width: 80px;
                        font-size: 11px;
                    }
                    QPushButton:hover {
                        background-color: #fa5252;
                    }
                """)
                
                msg_box.exec_()
                
                if msg_box.clickedButton() == yes_btn:
                    QDesktopServices.openUrl(QUrl(f"https://github.com/{APP_REPO}/releases"))
            else:
                # 已是最新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle("检查更新")
                msg_box.setText("当前已是最新版本")
                msg_box.setIcon(QMessageBox.NoIcon)
                msg_box.setStyleSheet("""
                    QMessageBox {
                        font-size: 11px;
                    }
                    QMessageBox QLabel {
                        color: #495057;
                        font-size: 11px;
                        padding: 10px;
                    }
                """)
                msg_box.exec_()
        
        except urllib.error.URLError as e:
            QMessageBox.warning(self, "检查更新", f"网络错误：{e}\n请检查网络连接")
        except Exception as e:
            QMessageBox.warning(self, "检查更新", f"检查更新失败：{e}")