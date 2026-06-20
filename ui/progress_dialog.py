"""
进度对话框
"""
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QProgressBar, QPushButton
)
from PySide6.QtCore import Qt, Signal

from i18n import I18n
from ui.widgets import AnimatedButton, BUTTON_STYLES


class ProgressDialog(QDialog):
    """进度对话框"""
    
    cancelled = Signal()
    
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.Dialog | Qt.WindowTitleHint | Qt.CustomizeWindowHint)
        self.setModal(False)
        self.setMinimumWidth(400)
        self._allow_close = False
        
        self._init_ui()
    
    def _init_ui(self):
        """初始化界面"""
        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        layout.setContentsMargins(20, 20, 20, 20)
        
        # 文件名标签
        self.filename_label = QLabel("")
        self.filename_label.setWordWrap(True)
        layout.addWidget(self.filename_label)
        
        # 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p%")
        layout.addWidget(self.progress_bar)
        
        # 状态标签
        self.status_label = QLabel("")
        self.status_label.setStyleSheet("color: gray;")
        layout.addWidget(self.status_label)
        
        # 取消按钮
        self.cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self.cancel_btn.clicked.connect(self._on_cancel)
        self.cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        layout.addWidget(self.cancel_btn, alignment=Qt.AlignRight)
    
    def set_filename(self, filename: str):
        """设置文件名"""
        self.filename_label.setText(filename)
    
    def set_progress(self, value: int, status: str = ""):
        """设置进度"""
        self.progress_bar.setValue(value)
        if status:
            self.status_label.setText(status)
    
    def set_size_info(self, current: int, total: int):
        """设置大小信息"""
        current_str = self._format_size(current)
        total_str = self._format_size(total)
        self.status_label.setText(f"{current_str} / {total_str}")
    
    def _format_size(self, size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} PB"
    
    def _on_cancel(self):
        """取消按钮点击"""
        self.cancelled.emit()
        self.reject()
    
    def allow_close(self):
        """允许关闭对话框"""
        self._allow_close = True
    
    def closeEvent(self, event):
        """关闭事件"""
        if self._allow_close:
            event.accept()
        else:
            # 阻止直接关闭，必须通过取消按钮
            event.ignore()


class CopyProgressDialog(ProgressDialog):
    """文件复制进度对话框"""
    
    def __init__(self, parent=None):
        super().__init__(I18n.tr('copying_files'), parent)


class SyncProgressDialog(ProgressDialog):
    """同步进度对话框"""
    
    def __init__(self, parent=None):
        super().__init__(I18n.tr('syncing_files'), parent)
