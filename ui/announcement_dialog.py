"""
公告详情窗口 - 完整展示公告正文（可选中复制）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit
)
from PySide6.QtCore import Qt

from i18n import I18n
from ui.widgets import AnimatedButton, BUTTON_STYLES


class AnnouncementDialog(QDialog):
    """公告详情弹窗：完整正文（只读、可选中复制）。

    主界面公告入口点击后打开；不展示版本号等内部信息，正文按窗口宽度自动换行。
    """

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('announcement'))
        flags = Qt.Dialog | Qt.WindowCloseButtonHint
        self.setWindowFlags(flags)
        self.setMinimumSize(420, 320)
        self.resize(460, 360)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(20, 18, 20, 16)

        # 标题
        title = QLabel(I18n.tr('announcement'))
        title.setStyleSheet("font-size: 16px; font-weight: bold;")
        layout.addWidget(title)

        # 正文（只读、可选中复制、自动换行；无边框透明背景，避免指示线）
        body = QPlainTextEdit()
        body.setPlainText(text)
        body.setReadOnly(True)
        body.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        body.setStyleSheet(
            "QPlainTextEdit { border: none; background: transparent; }"
        )
        layout.addWidget(body, 1)

        # 关闭按钮（全局取消样式：灰底白字，与进度/权限对话框一致）
        close_btn = AnimatedButton(I18n.tr('close'))
        close_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(self.accept)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)
