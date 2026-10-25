"""
首次启动 / 协议更新确认对话框
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLayout, QApplication, QFrame,
    QWidget
)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import (QPalette, QPixmap, QPainter, QColor, QFont, QIcon,
                           QFontMetrics)

from i18n import I18n
from config import Config, UserConfig
from ui.widgets import AnimatedButton, BUTTON_STYLES

_DIALOG_WIDTH = 468
_CONTENT_WIDTH = 412  # 468 - 左右外边距 28*2


class EulaDialog(QDialog):
    """用户协议与隐私政策确认弹窗（点击同意后写入协议版本号）。

    首次启动显示欢迎标题与产品定位；协议版本变更后显示更新标题，
    并在正文卡片内标出变更的协议。关闭窗口或点「不同意并退出」都返回
    Rejected，由调用方结束程序。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(Config.APP_NAME)
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setModal(True)

        self._first_run = not (
            UserConfig.get_terms_accepted_version()
            or UserConfig.get_privacy_accepted_version())
        self._terms_changed = (
            UserConfig.get_terms_accepted_version() != Config.TERMS_VERSION)
        self._privacy_changed = (
            UserConfig.get_privacy_accepted_version() != Config.PRIVACY_VERSION)

        self._init_ui()

    # ---------- 外部接口 ----------

    @staticmethod
    def needs_consent() -> bool:
        """用户尚未确认过当前版本的任一份协议时返回 True"""
        return (UserConfig.get_terms_accepted_version() != Config.TERMS_VERSION
                or UserConfig.get_privacy_accepted_version()
                != Config.PRIVACY_VERSION)

    # ---------- 界面 ----------

    def _init_ui(self):
        dark = self._is_dark_theme()
        title_color = "#e9ecef" if dark else "#212529"
        muted_color = "#adb5bd" if dark else "#868e96"
        body_color = "#ced4da" if dark else "#495057"
        link_color = "#4dabf7" if dark else "#339af0"
        accent_color = "#ffa94d" if dark else "#e8590c"

        self.setFixedWidth(_DIALOG_WIDTH)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 24, 28, 20)
        outer.setSpacing(0)
        outer.setSizeConstraint(QLayout.SetFixedSize)

        outer.addLayout(self._build_header(title_color, muted_color))
        outer.addSpacing(20)
        outer.addWidget(self._build_notice_block(
            body_color, link_color, accent_color))
        outer.addSpacing(18)
        outer.addWidget(self._build_separator(dark))
        outer.addSpacing(14)
        outer.addLayout(self._build_buttons())

    def _build_header(self, title_color: str, subtitle_color: str) -> QHBoxLayout:
        """应用图标 + 标题 + 一行副标题（首次为产品定位，更新为变更说明）"""
        row = QHBoxLayout()
        row.setSpacing(14)

        icon_label = QLabel()
        icon_label.setFixedSize(48, 48)
        icon_label.setPixmap(self._render_icon())
        row.addWidget(icon_label, 0, Qt.AlignVCenter)

        text_col = QVBoxLayout()
        text_col.setSpacing(4)

        title_text = (
            I18n.tr('eula_welcome_title', app=Config.APP_NAME)
            if self._first_run else I18n.tr('eula_update_title'))
        title = QLabel(title_text)
        title.setWordWrap(True)
        title.setFixedWidth(_CONTENT_WIDTH - 62)
        title.setStyleSheet(
            f"font-size: 17px; font-weight: 600; color: {title_color};")
        title.setMinimumHeight(title.heightForWidth(_CONTENT_WIDTH - 62))
        text_col.addWidget(title)

        subtitle = QLabel(
            I18n.tr('app_title') if self._first_run
            else I18n.tr('eula_update_hint'))
        subtitle.setWordWrap(True)
        subtitle.setFixedWidth(_CONTENT_WIDTH - 62)
        subtitle.setStyleSheet(
            f"font-size: 12px; color: {subtitle_color};")
        subtitle.setMinimumHeight(
            subtitle.heightForWidth(_CONTENT_WIDTH - 62))
        text_col.addWidget(subtitle)

        row.addLayout(text_col, 1)
        return row

    def _build_notice_block(self, body_color: str, link_color: str,
                            accent_color: str) -> QWidget:
        """协议说明直接放在对话框背景上，避免卡片套卡片"""
        block = QWidget()
        layout = QVBoxLayout(block)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 协议更新时标出具体变了哪一份（首次启动不显示）
        if not self._first_run and (self._terms_changed or self._privacy_changed):
            changed = []
            if self._terms_changed:
                changed.append(I18n.tr('eula_terms_updated'))
            if self._privacy_changed:
                changed.append(I18n.tr('eula_privacy_updated'))
            changed_label = QLabel(" · ".join(changed))
            changed_label.setStyleSheet(
                f"font-size: 12px; color: {accent_color};")
            layout.addWidget(changed_label)

        notice = QLabel(self._build_notice_html(link_color, body_color))
        notice.setTextFormat(Qt.RichText)
        notice.setOpenExternalLinks(True)
        notice.setWordWrap(True)
        notice.setFixedWidth(_CONTENT_WIDTH)
        notice.setMinimumHeight(notice.heightForWidth(_CONTENT_WIDTH))
        layout.addWidget(notice)

        return block

    def _build_separator(self, dark: bool) -> QFrame:
        """按钮区上方的细分隔线，用于收束内容而不增加卡片感"""
        line = QFrame()
        line.setFixedHeight(1)
        color = "rgba(255, 255, 255, 0.10)" if dark else "#dee2e6"
        line.setStyleSheet(f"QFrame {{ background: {color}; border: none; }}")
        return line

    def _build_buttons(self) -> QHBoxLayout:
        """按钮右对齐，主操作在右，次级操作在左"""
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addStretch()

        decline_text = I18n.tr('eula_decline_exit')
        agree_text = I18n.tr('eula_agree_continue')

        decline_btn = AnimatedButton(decline_text)
        decline_btn.setFixedSize(self._button_width(decline_text, 118), 38)
        decline_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        decline_btn.clicked.connect(self.reject)
        row.addWidget(decline_btn)

        agree_btn = AnimatedButton(agree_text)
        agree_btn.setFixedSize(self._button_width(agree_text, 132), 38)
        agree_btn.setStyleSheet(BUTTON_STYLES['primary'])
        agree_btn.setDefault(True)
        agree_btn.clicked.connect(self._on_accept)
        row.addWidget(agree_btn)

        return row

    @staticmethod
    def _button_width(text: str, minimum: int) -> int:
        """按按钮字号测宽（stylesheets 为 13px），保证英文文案不被裁切"""
        font = QFont()
        font.setPixelSize(13)
        text_width = QFontMetrics(font).horizontalAdvance(text)
        return min(max(minimum, text_width + 34), 190)

    def _build_notice_html(self, link_color: str, body_color: str) -> str:
        """拼出带内联链接的说明文字（链接由 QLabel 交给系统浏览器打开）"""
        terms = self._link_text(
            Config.TERMS_URL, I18n.tr('about_terms'), link_color)
        privacy = self._link_text(
            Config.PRIVACY_URL, I18n.tr('about_privacy'), link_color)
        text = I18n.tr(
            'eula_continue_notice', terms=terms, privacy=privacy)
        return (f'<div style="color: {body_color}; font-size: 14px; '
                f'line-height: 22px;">{text}</div>')

    @staticmethod
    def _link_text(url: str, name: str, color: str) -> str:
        label = f"《{name}》" if I18n.get_language() == "zh_CN" else name
        return (f'<a href="{url}" style="color: {color}; '
                f'text-decoration: none;">{label}</a>')

    def _render_icon(self) -> QPixmap:
        """优先使用窗口图标；离屏测试等无图标场景生成品牌字母块"""
        app = QApplication.instance()
        icon = app.windowIcon() if app is not None else QIcon()
        if not icon.isNull():
            pix = icon.pixmap(QSize(48, 48))
            if not pix.isNull():
                return pix

        pix = QPixmap(48, 48)
        pix.fill(Qt.transparent)
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#339af0"))
        painter.drawRoundedRect(0, 0, 48, 48, 12, 12)
        font = QFont()
        font.setPixelSize(26)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(pix.rect(), Qt.AlignCenter, "L")
        painter.end()
        return pix

    @staticmethod
    def _is_dark_theme() -> bool:
        """按应用调色板 Window 亮度判断深浅色主题"""
        app = QApplication.instance()
        if app is None:
            return False
        bg = app.palette().color(QPalette.Window)
        return (bg.red() * 0.299 + bg.green() * 0.587
                + bg.blue() * 0.114) < 128

    # ---------- 行为 ----------

    def _on_accept(self):
        """记录两份协议版本号后关闭（写入失败也不阻断用户进入程序）"""
        UserConfig.set_accepted_legal_versions(
            Config.TERMS_VERSION, Config.PRIVACY_VERSION)
        self.accept()

    def keyPressEvent(self, event):
        """忽略 Esc：避免误触直接退出程序（需明确点击「不同意并退出」）"""
        if event.key() == Qt.Key_Escape:
            return
        super().keyPressEvent(event)

    def showEvent(self, event):
        """独立窗口没有父级，显式居中到当前屏幕"""
        super().showEvent(event)
        screen = self.screen()
        if screen is not None:
            geo = screen.availableGeometry()
            self.move(geo.center() - self.rect().center())
