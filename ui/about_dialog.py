"""
关于对话框 - 包含项目信息和检查更新功能
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import re
import urllib.request
import ssl
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

        # SSL 上下文（避免 SSL 证书校验错误导致无法更新）
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.check_hostname = False
        self.ssl_context.verify_mode = ssl.CERT_NONE

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

        # 版本信息（版本号后拼接串号）
        version_label = QLabel(f"{I18n.tr('about_version_label')} {Config.APP_VERSION_SERIAL}")
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
        author_label = ClickableLabel(
            f"{I18n.tr('about_author')}: {Config.APP_AUTHOR}",
            normal_color="#495057",
            hover_color="#339af0",
            underline_on_hover=False
        )
        layout.addWidget(author_label)

        # GitHub链接（灰色悬浮变蓝，可点击打开仓库）
        github_label = ClickableLabel(
            f"GitHub: {Config.GITHUB_REPO}",
            normal_color="#495057",
            hover_color="#339af0",
            underline_on_hover=False
        )
        github_label.set_click_callback(self._open_github)
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

    def _get_latest_version(self):
        """从 GitHub Pages 纯文本文件拉取最新版本号
        返回值: (版本号字符串, 错误信息字符串) 元组
            成功时: ("R7.1.1.0", None)
            失败时: (None, "错误描述")
        """
        req = urllib.request.Request(Config.UPDATE_URL)
        req.add_header('User-Agent', Config.APP_NAME)

        try:
            with urllib.request.urlopen(req, timeout=15, context=self.ssl_context) as response:
                body = response.read().decode('utf-8').strip()
            # io 文件为 R 前缀四段（如 R7.1.1.0），校验后直接返回。
            if not re.match(r'R\d+(\.\d+){0,3}', body):
                return None, None
            return body, None
        except Exception as e:
            return None, str(e)

    def _check_update(self):
        """检查更新（从 github.io 拉取版本号，下载落地页按语言区分）"""
        # 拉取远程最新版本
        latest, err = self._get_latest_version()
        if not latest:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                err or I18n.tr('about_remote_parse_error'),
                QMessageBox.Warning
            )
            return

        # 下载落地页按语言区分（中文 Gitee / 其他 GitHub）
        releases_url = Config.GITEE_RELEASES if I18n.get_language() == "zh_CN" else Config.GITHUB_RELEASES

        # 解析远程与当前版本号（四段式元组比较）
        latest_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', latest)
        current_match = re.search(r'R(\d+)\.(\d+)\.(\d+)\.(\d+)', Config.APP_VERSION)
        if not latest_match:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                I18n.tr('about_remote_parse_error'),
                QMessageBox.Warning
            )
            return
        if not current_match:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                I18n.tr('about_parse_error'),
                QMessageBox.Warning
            )
            return

        latest_version = tuple(map(int, latest_match.groups()))
        current_version = tuple(map(int, current_match.groups()))

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
