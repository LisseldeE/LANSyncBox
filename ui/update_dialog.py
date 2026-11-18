"""
更新对话框
展示新版本与更新日志，下载安装包并调用安装程序。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import subprocess
import threading
from html import escape
from pathlib import Path

from PySide6.QtCore import (
    Property, QEasingCurve, QPropertyAnimation, QRectF, Qt, QTimer, Signal
)
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QTextBrowser,
    QVBoxLayout, QWidget
)

from config import Config
from i18n import I18n
from ui.widgets import AnimatedButton, BUTTON_STYLES
from utils import update_manager


class _ProgressBar(QWidget):
    """细进度条：确定态按百分比填充，不确定态自绘条纹（必定从左侧 0 点起步）。

    不用 QProgressBar 的忙态，因为它的相位来自样式的时间动画，切进去时
    可能落在轨道中间，无法通过 setRange 复位。
    """

    CHUNK_RATIO = 0.25
    CYCLE_MS = 3200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self._value = 0
        self._busy = False
        self._phase_value = 0.0
        self._anim = QPropertyAnimation(self, b'_phase', self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setDuration(self.CYCLE_MS)
        self._anim.setEasingCurve(QEasingCurve.Linear)
        self._anim.setLoopCount(-1)

    def _get_phase(self):
        return self._phase_value

    def _set_phase(self, value):
        self._phase_value = value
        self.update()

    _phase = Property(float, _get_phase, _set_phase)

    def setRange(self, minimum, maximum):
        busy = maximum == minimum
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._phase_value = 0.0  # 从 0 点起步
            self._anim.start()
        else:
            self._anim.stop()
        self.update()

    def setValue(self, value):
        self._value = max(0, min(100, int(value)))
        if not self._busy:
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 30))
        painter.drawRoundedRect(rect, radius, radius)

        if self._busy:
            chunk = max(48.0, rect.width() * self.CHUNK_RATIO)
            x = -chunk + self._phase * (rect.width() + chunk)
            fill = QRectF(x, 0, chunk, rect.height())
        elif self._value > 0:
            fill = QRectF(0, 0, rect.width() * self._value / 100.0, rect.height())
        else:
            return

        clip = QPainterPath()
        clip.addRoundedRect(rect, radius, radius)
        painter.setClipPath(clip)
        painter.setBrush(QColor('#339af0'))
        painter.drawRect(fill)


class UpdateDialog(QDialog):
    """新版本详情：版本号 + 更新日志 + 下载进度；完成后退出并启动安装包。"""

    progress_changed = Signal(int, int)  # (已下载字节, 总字节；0 表未知)
    verify_changed = Signal()            # 字节下载完成，进入 sha256 校验
    download_done = Signal(object)       # Path（成功）或 Exception（失败）

    def __init__(self, info: update_manager.UpdateInfo, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('update_title'))
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        self.setFixedSize(480, 430)

        self._info = info
        self._cancel = threading.Event()
        self._thread = None
        self._closed = False

        self._init_ui()
        self.progress_changed.connect(self._on_progress)
        self.verify_changed.connect(self._on_verify)
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
        self._log_view.setFrameShape(QFrame.NoFrame)
        self._log_view.setStyleSheet(
            'QTextBrowser { border: none; background: transparent; '
            'font-size: 12px; padding: 0; }'
            'QTextBrowser QWidget { background: transparent; }')
        if self._info.changelog:
            self._log_view.setHtml(''.join(
                f'<p style="margin:4px 0;">· {escape(item)}</p>'
                for item in self._info.changelog))
        else:
            self._log_view.setPlainText(I18n.tr('update_empty_log'))
        layout.addWidget(self._log_view, 1)

        self._progress = _ProgressBar()
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._status = QLabel('')
        self._status.setStyleSheet('font-size: 11px; color: #868e96;')
        layout.addWidget(self._status)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self._cancel_btn = AnimatedButton(I18n.tr('update_cancel'))
        self._cancel_btn.setFixedSize(120, 36)
        self._cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        self._cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(self._cancel_btn)

        btn_layout.addSpacing(8)

        self._download_btn = AnimatedButton(I18n.tr('update_download'))
        self._download_btn.setFixedSize(120, 36)
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
        self._download_btn.setText(I18n.tr('update_download'))
        self._download_btn.setEnabled(False)
        self._progress.setVisible(True)
        # 先给确定态的 0%，等确实拿不到总长度（total=0）再切不确定态，
        # 避免"条纹动画跑一半就跳成百分比"的视觉闪跳
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
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
                cancel=self._cancel,
                on_verify=lambda: self.verify_changed.emit())
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

    def _on_verify(self):
        """字节下载完成，进入 sha256 校验（安装包较大时需要一点时间）"""
        self._progress.setRange(0, 100)
        self._progress.setValue(100)
        self._status.setText(I18n.tr('update_verifying'))

    def _on_download_done(self, result):
        if self._closed:
            return
        if isinstance(result, Path):
            self._progress.setRange(0, 100)
            self._progress.setValue(100)
            self._status.setText(I18n.tr('update_launching'))
            QTimer.singleShot(400, lambda: self._launch_installer(result))
            return

        # 失败：按钮改为重试，小字说明原因
        self._download_btn.setEnabled(True)
        self._download_btn.setText(I18n.tr('update_retry'))
        self._progress.setVisible(False)
        self._status.setText(I18n.tr('update_failed', error=str(result)))

    def _launch_installer(self, path: Path):
        """延迟拉起安装包再退出主程序：安装器启动时旧 exe 已释放，避免文件占用。

        静默参数：不显示向导；安装器内部会静默卸载旧版并全量安装，结束后由
        [Run] 自行启动新版主程序（安装目录可能已改名，这里算不出新路径）。
        """
        if self._closed:
            return
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
        # 模态对话框的 exec 循环不会被 QApplication.quit() 打断：先结束对话框，
        # 回到主事件循环后再退出，确保进程在安装器启动前释放 exe/文件句柄。
        self.accept()
        QTimer.singleShot(0, QApplication.quit)

    def reject(self):
        self._closed = True
        self._cancel.set()
        super().reject()
