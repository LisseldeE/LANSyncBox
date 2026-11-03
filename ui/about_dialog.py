"""
关于对话框 - 包含项目信息和检查更新功能
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QMessageBox
)
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices

from i18n import I18n
from config import Config
from ui.widgets import AnimatedButton, BUTTON_STYLES, ClickableLabel
from ui.update_dialog import UpdateDialog
from utils.update_manager import fetch_manifest, is_newer


class AboutDialog(QDialog):
    """关于弹窗"""

    update_checked = Signal(object, str)  # 后台检查更新结果（UpdateInfo 或 None, 错误信息）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('about_title'))
        # 明确设置窗口标志，确保包含关闭按钮
        flags = Qt.Dialog | Qt.WindowCloseButtonHint
        self.setWindowFlags(flags)
        self.setFixedSize(400, 284)  # 增加一行协议链接后的紧凑高度

        self._check_update_btn = None
        self._update_check_pending = False

        self._init_ui()
        self.update_checked.connect(self._on_update_checked)

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

        # 官方网站链接
        details_label = ClickableLabel(
            I18n.tr('about_details'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        details_label.set_click_callback(self._open_details)
        link_layout.addWidget(details_label)

        link_layout.addSpacing(20)  # 两个链接之间间距

        # 支持作者链接
        support_label = ClickableLabel(
            I18n.tr('about_support_author'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        support_label.set_click_callback(self._open_support)
        link_layout.addWidget(support_label)

        link_layout.addStretch()
        layout.addLayout(link_layout)

        # 法律条款链接（用户协议 / 隐私政策，与上方功能链接分行居中）
        legal_layout = QHBoxLayout()
        legal_layout.addStretch()

        terms_label = ClickableLabel(
            I18n.tr('about_terms'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        terms_label.set_click_callback(self._open_terms)
        legal_layout.addWidget(terms_label)

        legal_sep = QLabel("·")
        legal_sep.setStyleSheet("font-size: 11px; color: #adb5bd;")
        legal_layout.addWidget(legal_sep)

        privacy_label = ClickableLabel(
            I18n.tr('about_privacy'),
            normal_color="#339af0",
            hover_color="#228be6",
            underline_on_hover=True
        )
        privacy_label.set_click_callback(self._open_privacy)
        legal_layout.addWidget(privacy_label)

        legal_layout.addStretch()
        layout.addLayout(legal_layout)

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
            self._check_update_btn = check_update_btn

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
        """打开 GitHub 仓库链接"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{Config.GITHUB_REPO}"))

    def _open_issues(self, event):
        """打开 GitHub Issues 页面（问题反馈）"""
        QDesktopServices.openUrl(QUrl(f"https://github.com/{Config.GITHUB_REPO}/issues"))

    def _open_details(self, event):
        """打开官方网站（官方网站链接）"""
        QDesktopServices.openUrl(QUrl("https://lansyncbox.com"))

    def _open_support(self, event):
        """打开爱发电赞助页（支持作者）"""
        QDesktopServices.openUrl(QUrl("https://ifdian.net/a/lisseldee"))

    def _open_terms(self, event):
        """打开用户协议页面"""
        QDesktopServices.openUrl(QUrl(Config.TERMS_URL))

    def _open_privacy(self, event):
        """打开隐私政策页面"""
        QDesktopServices.openUrl(QUrl(Config.PRIVACY_URL))

    def _show_styled_message(self, title: str, text: str, icon_type=QMessageBox.Information):
        """显示统一风格的提示框"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle(title)
        msg_box.setText(text)
        msg_box.setIcon(icon_type)
        ok_btn = msg_box.addButton(I18n.tr('ok'), QMessageBox.AcceptRole)
        ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
        msg_box.exec_()

    def _fetch_update(self):
        """拉取更新清单（网络/解析异常统一转为错误文本，不向 UI 线程抛出）"""
        try:
            return fetch_manifest(), None
        except Exception as e:
            return None, str(e)

    def _check_update(self):
        """检查更新：网络请求放后台线程，避免 UI 线程被最长 15 秒的超时阻塞"""
        if self._update_check_pending or self._check_update_btn is None:
            return
        self._update_check_pending = True
        button = self._check_update_btn
        button.setEnabled(False)
        button.setText(I18n.tr('about_checking'))

        def _fetch():
            info, err = self._fetch_update()
            try:
                self.update_checked.emit(info, err or "")
            except RuntimeError:
                pass  # 窗口已随程序退出被销毁

        threading.Thread(target=_fetch, daemon=True,
                         name="about-update-check").start()

    def _on_update_checked(self, info, err: str):
        """后台检查完成：恢复按钮并展示结果（窗口已关闭时只恢复状态）"""
        self._update_check_pending = False
        if self._check_update_btn is not None:
            self._check_update_btn.setText(I18n.tr('about_check_update'))
            self._check_update_btn.setEnabled(True)
        if not self.isVisible():
            return
        self._show_update_result(info, err)

    def _show_update_result(self, info, err: str):
        """按检查结果弹出提示（发现新版本 → 更新对话框 / 已是最新 / 失败）"""
        if info is None:
            self._show_styled_message(
                I18n.tr('about_check_update'),
                err or I18n.tr('about_remote_parse_error'),
                QMessageBox.Warning
            )
            return

        if not is_newer(info.version, Config.APP_VERSION):
            self._show_styled_message(
                I18n.tr('about_check_update'),
                I18n.tr('about_latest'),
                QMessageBox.Information
            )
            return

        # 发现新版本：交给更新对话框展示日志并下载安装包
        UpdateDialog(info, self).exec_()
