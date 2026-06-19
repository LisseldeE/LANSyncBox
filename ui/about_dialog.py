# -*- coding: utf-8 -*-
"""
LANSyncBox 关于对话框 - 支持中英文切换和双端API
"""

import urllib.request
import urllib.error
import json
import re

from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QMessageBox, QApplication
)
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtGui import QDesktopServices

from config import (
    STYLESHEET, APP_NAME, APP_VERSION, APP_AUTHOR, APP_EMAIL,
    APP_REPO, APP_REPO_GITEE, GITHUB_API, GITEE_API,
    GITHUB_RELEASES, GITEE_RELEASES
)
from i18n import I18n


class AboutDialog(QDialog):
    """关于弹窗 - 支持中英文切换"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.t('about_title'))
        # 移除右上角的问号按钮
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setFixedSize(400, 280)
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
        version_label = QLabel(f"{I18n.t('about_version')}：{APP_VERSION}")
        version_label.setStyleSheet("font-size: 11px; color: #495057;")
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label)
        
        # 描述
        desc_label = QLabel(I18n.t('about_desc'))
        desc_label.setStyleSheet("font-size: 10px; color: #868e96;")
        desc_label.setAlignment(Qt.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)
        
        layout.addSpacing(8)
        
        # 作者信息
        author_label = QLabel(f"{I18n.t('about_author')}：{APP_AUTHOR}")
        author_label.setStyleSheet("font-size: 10px; color: #495057;")
        author_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(author_label)
        
        # GitHub链接（始终显示GitHub）
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
        check_update_btn = QPushButton(I18n.t('about_check_update'))
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
        close_btn = QPushButton(I18n.t('about_close'))
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
        """检查更新 - 根据语言选择API源"""
        try:
            # 根据语言选择API端点
            if I18n.get_lang() == 'zh':
                api_url = GITEE_API
                releases_url = GITEE_RELEASES
            else:
                api_url = GITHUB_API
                releases_url = GITHUB_RELEASES
            
            # 创建请求，添加User-Agent
            req = urllib.request.Request(api_url)
            req.add_header('User-Agent', APP_NAME)
            
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())
            
            if not data:
                QMessageBox.information(
                    self, 
                    I18n.t('about_check_update'),
                    I18n.t('about_no_tags')
                )
                return
            
            # 遍历所有tags，找到版本号最大的那个
            # 注意：Gitee API返回的tags可能不是按版本号排序
            latest_tag = None
            latest_version_num = -1
            
            for tag in data:
                tag_name = tag.get('name', '')
                version_match = re.search(r'R(\d+)', tag_name)
                if version_match:
                    version_num = int(version_match.group(1))
                    if version_num > latest_version_num:
                        latest_version_num = version_num
                        latest_tag = tag_name
            
            if latest_tag is None:
                QMessageBox.information(
                    self,
                    I18n.t('about_check_update'),
                    I18n.t('about_no_tags')
                )
                return
            
            # 解析当前版本号
            current_version_match = re.search(r'R(\d+)', APP_VERSION)
            if not current_version_match:
                QMessageBox.warning(
                    self,
                    I18n.t('about_check_update'),
                    I18n.t('about_parse_error')
                )
                return
            current_version = int(current_version_match.group(1))
            
            # 比较版本号
            if latest_version_num > current_version:
                # 发现新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.t('about_check_update'))
                msg_box.setText(I18n.t('about_new_version', latest_tag))
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
                yes_btn = msg_box.addButton(I18n.t('about_yes'), QMessageBox.YesRole)
                no_btn = msg_box.addButton(I18n.t('about_no'), QMessageBox.NoRole)
                
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
                    # 打开对应平台的Releases页面
                    QDesktopServices.openUrl(QUrl(releases_url))
            else:
                # 已是最新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.t('about_check_update'))
                msg_box.setText(I18n.t('about_latest'))
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
            QMessageBox.warning(
                self,
                I18n.t('about_check_update'),
                I18n.t('about_network_error', str(e))
            )
        except Exception as e:
            QMessageBox.warning(
                self,
                I18n.t('about_check_update'),
                I18n.t('about_check_failed', str(e))
            )