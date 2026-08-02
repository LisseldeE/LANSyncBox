"""
关于对话框 - 包含项目信息和检查更新功能
Copyright (c) 2026 Lisselde_E.
Licensed under the GNU General Public License v3.0.
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
from ui.widgets import AnimatedButton, BUTTON_STYLES, ClickableLabel


class AboutDialog(QDialog):
    """关于弹窗"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('about_title'))
        # 明确设置窗口标志，确保包含关闭按钮
        flags = Qt.Dialog | Qt.WindowCloseButtonHint
        self.setWindowFlags(flags)
        self.setFixedSize(400, 260)  # 更紧凑的高度
        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(6)  # 更紧凑的间距
        layout.setContentsMargins(20, 15, 20, 15)  # 更紧凑的边距

        # 标题
        title_label = QLabel(Config.APP_NAME)
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
        version_label = QLabel(f"{I18n.tr('about_version_label')} {Config.APP_VERSION}")
        version_label.setStyleSheet("font-size: 12px; color: #495057;")
        version_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(version_label)

        # 描述
        desc_label = QLabel(I18n.tr('about_description'))
        desc_label.setStyleSheet("font-size: 11px; color: #868e96;")
        desc_label.setAlignment(Qt.AlignCenter)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # 作者信息（灰色悬浮变蓝，不可点击）
        author_label = QLabel(f"{I18n.tr('about_author')}: {Config.APP_AUTHOR}")
        author_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #495057;
            }
            QLabel:hover {
                color: #339af0;
            }
        """)
        author_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(author_label)

        # GitHub链接（灰色悬浮变蓝）
        github_label = QLabel(f"GitHub: {Config.GITHUB_REPO}")
        github_label.setStyleSheet("""
            QLabel {
                font-size: 11px;
                color: #495057;
            }
            QLabel:hover {
                color: #339af0;
            }
        """)
        github_label.setAlignment(Qt.AlignCenter)
        github_label.setCursor(Qt.PointingHandCursor)
        github_label.mousePressEvent = lambda event: self._open_github(event)
        layout.addWidget(github_label)

        # 问题反馈和查看详情链接（蓝色，悬浮加下划线）
        link_layout = QHBoxLayout()
        link_layout.addStretch()

        # 问题反馈链接
        feedback_label = ClickableLabel(
            I18n.tr('about_feedback'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        feedback_label.set_click_callback(self._open_issues)
        link_layout.addWidget(feedback_label)

        link_layout.addSpacing(20)  # 两个链接之间间距

        # 查看详情链接
        details_label = ClickableLabel(
            I18n.tr('about_details'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        details_label.set_click_callback(self._open_details)
        link_layout.addWidget(details_label)

        link_layout.addStretch()
        layout.addLayout(link_layout)

        # 按钮区域
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        if Config.ENABLE_CHECK_UPDATE:
            # 检查更新按钮（仅 GitHub 版本显示）
            check_update_btn = AnimatedButton(I18n.tr('about_check_update'))
            check_update_btn.setFixedSize(120, 36)
            check_update_btn.setStyleSheet(BUTTON_STYLES['primary'])
            check_update_btn.clicked.connect(self._check_update)
            btn_layout.addWidget(check_update_btn)

            btn_layout.addSpacing(8)  # 更紧凑的按钮间距

        # 关闭按钮
        close_btn = AnimatedButton(I18n.tr('close'))
        close_btn.setFixedSize(120, 36)
        close_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    def _open_github(self, event):
        """打开 GitHub 链接"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{Config.GITHUB_REPO}"))

    def _open_issues(self, event):
        """打开 GitHub Issues 页面（问题反馈）"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{Config.GITHUB_REPO}/issues"))

    def _open_details(self, event):
        """打开作者主页链接（查看详情）"""
        QDesktopServices.openUrl(QUrl(Config.APP_AUTHOR_LINK))

    def _show_styled_message(self, title: str, text: str, icon_type=QMessageBox.Information):
        """显示统一风格的提示框"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(icon_type)
        ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
        ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
        msg_box.exec_()

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
                self._show_styled_message(
                    I18n.tr('about_check_update'),
                    I18n.tr('about_no_tags'),
                    QMessageBox.Warning
                )
                return

            # 遍历所有 tags，找到版本号最大的那个
            latest_tag = None
            latest_version = (0, 0, 0, 0)

            for tag in data:
                tag_name = tag.get('name', '')
                version = None
                # 优先匹配 R6.9.1.0 四段式格式
                version_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', tag_name)
                if version_match:
                    version = tuple(map(int, version_match.groups()))
                else:
                    # 兼容 R6.x 旧格式，补齐为 R6.x.0.0
                    old_match = re.search(r'R(\d+)(?:\.(\d+))?', tag_name)
                    if old_match:
                        major = int(old_match.group(1))
                        minor = int(old_match.group(2)) if old_match.group(2) else 0
                        version = (major, minor, 0, 0)
                if version and version > latest_version:
                    latest_version = version
                    latest_tag = tag_name

            if latest_tag is None:
                self._show_styled_message(
                    I18n.tr('about_check_update'),
                    I18n.tr('about_remote_parse_error'),
                    QMessageBox.Warning
                )
                return

            # 解析当前版本号
            current_version_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', Config.APP_VERSION)
            if not current_version_match:
                self._show_styled_message(
                    I18n.tr('about_check_update'),
                    I18n.tr('about_parse_error'),
                    QMessageBox.Warning
                )
                return

            current_version = tuple(map(int, current_version_match.groups()))

            # 比较版本号（元组逐段比较）
            if latest_version > current_version:
                # 发现新版本
                msg_box = QMessageBox(self)
                msg_box.setWindowTitle(I18n.tr('about_check_update'))
                msg_box.setText(I18n.tr('about_new_version', version=f"R{'.'.join(map(str, latest_version))}"))
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
                self._show_styled_message(
                    I18n.tr('about_check_update'),
                    I18n.tr('about_latest'),
                    QMessageBox.Information
                )

        except urllib.error.URLError as e:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                I18n.tr('about_network_error', error=str(e)),
                QMessageBox.Warning
            )
        except Exception as e:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                I18n.tr('about_check_failed', error=str(e)),
                QMessageBox.Warning
            )
