"""
关于对话框 - 包含项目信息和检查更新功能
"""
import re
import urllib.request
import json
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QMessageBox, QApplication
)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices

from i18n import I18n
from config import Config
from ui.widgets import AnimatedButton, BUTTON_STYLES


class AboutDialog(QDialog):
    """关于弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('about_title'))
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowContextHelpButtonHint)
        self.setFixedSize(400, 320)
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(8)
        layout.setContentsMargins(25, 20, 25, 20)

        # 标题
        title_label = QLabel(Config.APP_NAME)
        title_label.setStyleSheet("""
            QLabel {
                font-size: 24px;
                font-weight: bold;
                color: #339af0;
            }
        """)
        title_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(title_label)

        # 版本信息
        version_label = QLabel(f"{I18n.tr('about_version_label')} {Config.DISPLAY_VERSION}")
        version_label.setStyleSheet("font-size: 13px; color: #495057;")
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label)

        # 描述
        desc_label = QLabel(I18n.tr('about_description'))
        desc_label.setStyleSheet("font-size: 12px; color: #868e96;")
        desc_label.setAlignment(Qt.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # 作者信息
        author_label = QLabel(f"{I18n.tr('about_author')}: {Config.APP_AUTHOR}")
        author_label.setStyleSheet("font-size: 12px; color: #495057;")
        author_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(author_label)

        # GitHub链接
        github_label = QLabel(f"GitHub: {Config.GITHUB_REPO}")
        github_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
                color: #339af0;
            }
            QLabel:hover {
                color: #228be6;
                text-decoration: underline;
            }
        """)
        github_label.setAlignment(Qt.AlignCenter)
        github_label.setCursor(Qt.PointingHandCursor)
        github_label.mousePressEvent = lambda event: self._open_github()
        layout.addWidget(github_label)

        # 邮箱
        email_label = QLabel(f"Email: {Config.APP_EMAIL}")
        email_label.setStyleSheet("""
            QLabel {
                font-size: 12px;
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

        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if Config.ENABLE_CHECK_UPDATE:
            # 检查更新按钮（仅 GitHub 版本显示）
            check_update_btn = AnimatedButton(I18n.tr('about_check_update'))
            check_update_btn.setMinimumWidth(120)
            check_update_btn.setFixedHeight(36)
            check_update_btn.setStyleSheet(BUTTON_STYLES['primary'])
            check_update_btn.clicked.connect(self._check_update)
            btn_layout.addWidget(check_update_btn)

            btn_layout.addSpacing(10)

        # 关闭按钮
        close_btn = AnimatedButton(I18n.tr('close'))
        close_btn.setFixedSize(100, 36)
        close_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def _open_github(self):
        """打开 GitHub 链接"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{Config.GITHUB_REPO}"))

    def _copy_email(self):
        """复制邮箱到剪贴板"""
        clipboard = QApplication.clipboard()
        clipboard.setText(Config.APP_EMAIL)
        QMessageBox.information(self, I18n.tr('about_info'), I18n.tr('about_email_copied'))

    def _check_update(self):
        """检查更新（根据语言选择 API 源）"""
        try:
            # 根据语言选择 API 端点
            if I18n.get_language() == "zh_CN":
                api_url = Config.GITEE_API
                releases_url = Config.GITEE_RELEASES
            else:
                api_url = Config.GITHUB_API
                releases_url = Config.GITHUB_RELEASES

            # 创建请求，添加 User-Agent
            req = urllib.request.Request(api_url)
            req.add_header('User-Agent', Config.APP_NAME)

            # 发送请求，设置超时时间
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode())

            if not data:
                QMessageBox.warning(self, I18n.tr('about_check_update'), I18n.tr('about_no_tags'))
                return

            # 遍历所有 tags，找到版本号最大的那个
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
                QMessageBox.warning(self, I18n.tr('about_check_update'), I18n.tr('about_remote_parse_error'))
                return

            # 解析当前版本号
            current_version_match = re.search(r'R(\d+)', Config.APP_VERSION)
            if not current_version_match:
                QMessageBox.warning(self, I18n.tr('about_check_update'), I18n.tr('about_parse_error'))
                return

            current_version = int(current_version_match.group(1))

            # 比较版本号
            if latest_version_num > current_version:
                # 发现新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.tr('about_check_update'))
                msg_box.setText(I18n.tr('about_new_version', version=latest_tag))
                msg_box.setIcon(QMessageBox.NoIcon)

                # 自定义按钮
                yes_btn = msg_box.addButton(I18n.tr('about_yes'), QMessageBox.YesRole)
                no_btn = msg_box.addButton(I18n.tr('about_no'), QMessageBox.NoRole)

                # 绿色"是"按钮
                yes_btn.setStyleSheet(BUTTON_STYLES['success'])

                # 红色"否"按钮
                no_btn.setStyleSheet(BUTTON_STYLES['danger'])

                msg_box.exec_()

                # 处理用户选择
                if msg_box.clickedButton() == yes_btn:
                    QDesktopServices.openUrl(QUrl(releases_url))
            else:
                # 已是最新版本
                QMessageBox.information(self, I18n.tr('about_check_update'), I18n.tr('about_latest'))

        except urllib.error.URLError as e:
            QMessageBox.warning(self, I18n.tr('about_check_update'), I18n.tr('about_network_error', error=str(e)))
        except Exception as e:
            QMessageBox.warning(self, I18n.tr('about_check_update'), I18n.tr('about_check_failed', error=str(e)))
