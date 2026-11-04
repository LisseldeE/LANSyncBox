"""
更新对话框 - 展示新版本与更新日志，下载安装包并调用安装程序
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import subprocess
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QProgressBar, QTextBrowser,
    QVBoxLayout
)

from config import Config
from i18n import I18n
from ui.widgets import AnimatedButton, BUTTON_STYLES
from utils import update_manager


class UpdateDialog(QDialog):
    """新版本详情：版本号 + 更新日志 + 下载进度；完成后退出并启动安装包。"""

    progress_changed = Signal(int, int)  # (已下载字节, 总字节；0 表未知)
    download_done = Signal(object)       # Path（成功）或 Exception（失败）

    def __init__(self, info: update_manager.UpdateInfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('update_title'))
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setFixedSize(480, 430)

        self._info = info
        self._cancel = threading.Event()
        self._thread = None
        self._prepared = False

        self._init_ui()
        self.progress_changed.connect(self._on_progress)
        self.download_done.connect(self._on_download_done)

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        title = QLabel(I18n.tr('update_new_version', version=self._info.version))
        title.setStyleSheet('font-size: 18px; font-weight: bold; color: #339af0;')
        layout.addWidget(title)

        current = QLabel(I18n.tr('update_current_version', version=Config.APP_VERSION))
        current.setStyleSheet('font-size: 11px; color: #868e96;')
        layout.addWidget(current)

        log_title = QLabel(I18n.tr('update_changelog'))
        log_title.setStyleSheet('font-size: 12px; font-weight: bold; color: #495057;')
        layout.addWidget(log_title)

        self._log_view = QTextBrowser()
        self._log_view.setOpenLinks(False)
        self._log_view.setStyleSheet(
            'QTextBrowser { border: 1px solid #dee2e6; border-radius: 6px; '
            'background: transparent; font-size: 12px; padding: 6px; }')
        if self._info.changelog:
            self._log_view.setHtml(''.join(
                f'<p style="margin:4px 0;">· {item}</p>' for item in self._info.changelog))
        else:
            self._log_view.setPlainText(I18n.tr('update_empty_log'))
        layout.addWidget(self._log_view, 1)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setTextVisible(False)
        self._progress.setFixedHeight(8)
        layout.addWidget(self._progress)

        self._status = QLabel('')
        self._status.setStyleSheet('font-size: 11px; color: #868e96;')
        layout.addWidget(self._status)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._later_btn = AnimatedButton(I18n.tr('update_later'))
        self._later_btn.setFixedSize(110, 36)
        self._later_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        self._later_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._later_btn)

        btn_layout.addSpacing(8)

        self._download_btn = AnimatedButton(I18n.tr('update_download'))
        self._download_btn.setFixedSize(150, 36)
        self._download_btn.setStyleSheet(BUTTON_STYLES['primary'])
        self._download_btn.clicked.connect(self._start_download)
        btn_layout.addWidget(self._download_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        self.setLayout(layout)

    # ---- 下载 ----

    def _start_download(self):
        if self._thread and self._thread.is_alive():
            return
        self._download_btn.setEnabled(False)
        self._progress.setVisible(True)
        self._progress.setRange(0, 0)
        self._status.setText(I18n.tr('update_downloading_unknown'))
        self._cancel.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True,
                                        name='update-download')
        self._thread.start()

    def _worker(self):
        try:
            path = update_manager.download_package(
                self._info, I18n.get_language(),
                progress=lambda done, total: self.progress_changed.emit(done, total),
                cancel=self._cancel)
        except Exception as e:
            self.download_done.emit(e)
            return
        self.download_done.emit(path)

    def _on_progress(self, done: int, total: int):
        if total > 0:
            percent = min(int(done * 100 / total), 100)
            self._progress.setRange(0, 100)
            self._progress.setValue(percent)
            self._status.setText(I18n.tr('update_downloading', percent=percent))
        else:
            self._progress.setRange(0, 0)
            self._status.setText(I18n.tr('update_downloading_unknown'))

    def _on_download_done(self, result):
        if isinstance(result, Path):
            self._prepared = True
            self._progress.setRange(0, 100)
            self._progress.setValue(100)
            self._status.setText(I18n.tr('update_launching'))
            QTimer.singleShot(400, lambda: self._launch_installer(result))
            return

        # 失败：恢复按钮，允许重试
        self._download_btn.setEnabled(True)
        self._progress.setVisible(False)
        self._status.setText(I18n.tr('update_failed', error=str(result)))

    def _launch_installer(self, path: Path):
        """延迟拉起安装包再退出主程序：安装器启动时旧 exe 已释放，避免文件占用。

        静默参数：不显示向导；安装器内部会静默卸载旧版并全量安装，结束后由
        [Run] 自行启动新版主程序（安装目录可能已改名，这里算不出新路径）。
        """
        command = (f'cmd /c ping -n 3 127.0.0.1 >nul & start "" "{path}" '
                   f'/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-')
        try:
            subprocess.Popen(
                command, shell=True,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as e:
            self._status.setText(I18n.tr('update_failed', error=str(e)))
            self._download_btn.setEnabled(True)
            return
        QApplication.quit()

    def reject(self):
        self._cancel.set()
        super().reject()
