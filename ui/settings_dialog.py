"""
设置对话框
主界面入口：左侧分类菜单（常规/系统/缓存）+ 右侧内容区，切换带淡出淡入动画
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QScrollArea, QFrame, QApplication, QMessageBox, QStackedWidget,
    QGraphicsOpacityEffect
)
from PySide6.QtCore import Qt, Signal, QRectF, QVariantAnimation, QAbstractAnimation, QEasingCurve, QUrl
from PySide6.QtGui import QPainter, QColor, QFont, QPalette, QDesktopServices

from config import Config, UserConfig
from i18n import I18n
from ui.widgets import ToggleSwitch, AnimatedButton, BUTTON_STYLES
from sync.file_manager import safe_rmtree


def _is_dark(widget=None) -> bool:
    """判断当前是否深色主题（基于窗口调色板亮度）"""
    win = widget.window() if widget is not None else None
    pal = win.palette() if win is not None else QApplication.palette()
    bg = pal.color(QPalette.Window)
    return (bg.red() * 0.299 + bg.green() * 0.587 + bg.blue() * 0.114) < 128


class _CategoryList(QWidget):
    """左侧分类菜单：自绘竖直胶囊列表，选中高亮 + 悬停反馈"""

    picked = Signal(int)

    ITEM_H, GAP, START = 38, 6, 4

    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []
        self._active = 0
        self._hover = -1
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(96)

    def set_items(self, titles):
        self.items = list(titles)
        self._active = min(self._active, len(self.items) - 1)
        self._hover = -1
        self.update()

    def set_active(self, idx):
        if idx is not None and idx != self._active:
            self._active = idx
            self.update()

    def _index_at(self, pos):
        if not self.items:
            return -1
        rel = pos.y() - self.START
        if rel < 0:
            return -1
        step = self.ITEM_H + self.GAP
        idx = rel // step
        return idx if idx < len(self.items) else -1

    def _item_rect(self, i):
        y = self.START + i * (self.ITEM_H + self.GAP)
        return QRectF(self.START, y, self.width() - 2 * self.START, self.ITEM_H)

    def paintEvent(self, _):
        if not self.items:
            return
        dark = _is_dark(self)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        accent = QColor(51, 154, 240)
        f = QFont(self.font())
        f.setPointSize(10)
        f.setWeight(QFont.Weight.DemiBold)
        p.setFont(f)
        for i, t in enumerate(self.items):
            r = self._item_rect(i)
            if i == self._active:
                sel = QColor(accent.red(), accent.green(), accent.blue(), 42)
                p.setPen(Qt.NoPen)
                p.setBrush(sel)
                p.drawRoundedRect(r, 8, 8)
                p.setPen(accent)
            elif i == self._hover:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#f1f3f5") if not dark else QColor("#3a3a3d"))
                p.drawRoundedRect(r, 8, 8)
                p.setPen(QColor("#212529") if not dark else QColor("#ced4da"))
            else:
                p.setPen(QColor("#868e96") if not dark else QColor("#a0a0a0"))
            p.drawText(r, Qt.AlignCenter, t)
        p.end()

    def mousePressEvent(self, e):
        idx = self._index_at(e.position().toPoint())
        if idx >= 0:
            self.picked.emit(idx)
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        h = self._index_at(e.position().toPoint())
        if h != self._hover:
            self._hover = h
            self.update()

    def leaveEvent(self, e):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(e)


class SettingsDialog(QDialog):
    """设置对话框

    左侧分类菜单（常规/系统/缓存）+ 右侧内容区，分类切换带淡出淡入过渡。
    - 常规：退出房间询问（= confirm_leave_no_ask 取反）、自动检查更新
      （Config.ENABLE_CHECK_UPDATE=False 时整行隐藏）
    - 系统：接收推送公告（仅开关，功能后续构建）
    - 缓存：列出 SyncFolder 下全部一级子目录（排除 preview，含空目录与
      非房间号干扰目录），每项显示名称/占用/「清空」按钮（删除整个目录），
      右上角「管理文件夹」按钮打开 SyncFolder
    """

    cache_changed = Signal()  # 缓存目录被清空，通知主窗刷新缓存占用
    announcements_toggled = Signal(bool)  # 「接收推送公告」开关变化（True=开启）

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(I18n.tr('settings'))
        # 非模态面板：带系统关闭按钮，不阻塞主窗口
        self.setWindowFlags(Qt.Dialog | Qt.WindowCloseButtonHint)
        # 尺寸参考创建房间对话框（固定宽度 400，高度可调整）
        self.setFixedWidth(400)
        self.setMinimumHeight(360)
        self.resize(400, 400)

        self._menu = None
        self._stack = None
        self._cat_anim = None
        self._auto_update_row = None
        self._toggles = {}
        self._cache_rows = {}          # 目录名 -> 行 widget
        self._cache_rows_layout = None
        self._cache_empty_label = None
        self._cache_watcher = None
        self._cache_refresh_timer = None

        self._init_ui()
        self._setup_cache_watcher()
        self._rebuild_cache_rows()

    # ---------- UI ----------

    def _init_ui(self):
        # 布局间距与创建/加入对话框一致（20px 外边距、15px 间距）
        root = QHBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(15)

        # 左侧分类菜单
        self._menu = _CategoryList(self)
        self._menu.set_items([
            I18n.tr('settings_category_general'),
            I18n.tr('settings_category_system'),
            I18n.tr('settings_category_cache'),
        ])
        self._menu.picked.connect(self._switch_category)
        root.addWidget(self._menu)

        # 右侧内容区（切换动画作用对象）
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._build_general_page())
        self._stack.addWidget(self._build_system_page())
        self._stack.addWidget(self._build_cache_page())
        root.addWidget(self._stack, 1)

        self._menu.set_active(0)
        self._stack.setCurrentIndex(0)

    def _build_general_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # 分组小标题（与创建/加入对话框的字段标签一致：灰色小字）
        lay.addWidget(self._make_section_label(I18n.tr('settings_section_general')))

        # 退出房间询问（= confirm_leave_no_ask 取反）
        leave_row, leave_sw = self._make_switch_row(
            I18n.tr('settings_confirm_leave_ask'),
            not UserConfig.get_confirm_leave_no_ask(),
            lambda checked: UserConfig.set_confirm_leave_no_ask(not checked),
        )
        lay.addWidget(leave_row)

        # 自动检查更新（Config.ENABLE_CHECK_UPDATE=False 时整行隐藏）
        self._auto_update_row, auto_sw = self._make_switch_row(
            I18n.tr('settings_auto_check_update'),
            UserConfig.get_auto_check_update(),
            lambda checked: UserConfig.set_auto_check_update(checked),
        )
        if not Config.ENABLE_CHECK_UPDATE:
            self._auto_update_row.setVisible(False)
        lay.addWidget(self._auto_update_row)

        lay.addStretch()
        return page

    def _build_system_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        lay.addWidget(self._make_section_label(I18n.tr('settings_section_notify')))

        # 接收推送公告（关闭后主界面不显示公告入口；重新开启时恢复已接收公告）
        announce_row, announce_sw = self._make_switch_row(
            I18n.tr('settings_receive_announcements'),
            UserConfig.get_receive_announcements(),
            lambda checked: UserConfig.set_receive_announcements(checked),
        )
        announce_sw.stateChanged.connect(self.announcements_toggled.emit)
        lay.addWidget(announce_row)

        lay.addStretch()
        return page

    def _build_cache_page(self):
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        # 缓存目录标题 + 管理文件夹按钮同一行（标题居左，按钮居右）
        top = QHBoxLayout()
        top.setSpacing(12)
        sec_label = self._make_section_label(I18n.tr('settings_section_cache'))
        top.addWidget(sec_label)
        top.setAlignment(sec_label, Qt.AlignVCenter)
        top.addStretch()
        manage_btn = AnimatedButton(I18n.tr('settings_manage_folders'))
        manage_btn.setFixedSize(110, 32)
        manage_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        manage_btn.clicked.connect(self._on_manage_folders)
        top.addWidget(manage_btn)
        lay.addLayout(top)

        # 缓存目录列表滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        container = QWidget()
        self._cache_rows_layout = QVBoxLayout(container)
        self._cache_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._cache_rows_layout.setSpacing(10)
        self._cache_rows_layout.addStretch()
        scroll.setWidget(container)
        lay.addWidget(scroll, stretch=1)
        return page

    def _make_section_label(self, text: str) -> QLabel:
        """分组小标题：灰色小字（与创建/加入对话框字段标签一致的层级感）"""
        dark = _is_dark(self)
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"font-size: 11px; color: {'#a0a0a0' if dark else '#868e96'};"
        )
        return lbl

    def _make_switch_row(self, label_text: str, checked: bool, on_changed):
        """构建「标签 + stretch + ToggleSwitch」单行，返回 (行widget, 开关)
        标签使用默认调色板文字色，与创建/加入对话框的字段标签一致。
        """
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 4, 0, 4)
        lay.setSpacing(12)
        label = QLabel(label_text)
        lay.addWidget(label)
        lay.addStretch()
        sw = ToggleSwitch(checked=checked)
        sw.stateChanged.connect(on_changed)
        lay.addWidget(sw)
        return row, sw

    # ---------- 分类切换动画 ----------

    def _switch_category(self, idx: int):
        if idx < 0 or idx >= self._stack.count() or idx == self._stack.currentIndex():
            return
        if self._cat_anim is not None:
            self._cat_anim.stop()
            self._cat_anim = None
        stack = self._stack
        eff = QGraphicsOpacityEffect(stack)
        stack.setGraphicsEffect(eff)

        def _finish_anim(a):
            if stack.graphicsEffect() is eff:
                stack.setGraphicsEffect(None)
            if self._cat_anim is a:
                self._cat_anim = None

        def _do_switch():
            self._menu.set_active(idx)
            stack.setCurrentIndex(idx)
            eff.setOpacity(0.0)
            a2 = QVariantAnimation(stack)
            a2.setDuration(130)
            a2.setStartValue(0.0)
            a2.setEndValue(1.0)
            a2.setEasingCurve(QEasingCurve.OutCubic)
            a2.valueChanged.connect(lambda v: eff.setOpacity(float(v)))
            a2.finished.connect(lambda: _finish_anim(a2))
            a2.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
            self._cat_anim = a2

        a1 = QVariantAnimation(stack)
        a1.setDuration(110)
        a1.setStartValue(1.0)
        a1.setEndValue(0.0)
        a1.setEasingCurve(QEasingCurve.OutCubic)
        a1.valueChanged.connect(lambda v: eff.setOpacity(float(v)))
        a1.finished.connect(lambda: (eff.setOpacity(0.0), _do_switch()))
        a1.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
        self._cat_anim = a1

    # ---------- 缓存页 ----------

    def _setup_cache_watcher(self):
        from PySide6.QtCore import QFileSystemWatcher, QTimer
        self._cache_watcher = QFileSystemWatcher(self)
        self._cache_refresh_timer = QTimer(self)
        self._cache_refresh_timer.setSingleShot(True)
        self._cache_refresh_timer.timeout.connect(self._rebuild_cache_rows)
        self._cache_watcher.directoryChanged.connect(self._delayed_refresh_cache)
        self._refresh_watcher()

    def _refresh_watcher(self):
        """重挂文件系统监听（目录被清空后旧路径失效，需 removePath 后重挂）"""
        try:
            dirs = self._cache_watcher.directories()
            if dirs:
                self._cache_watcher.removePaths(dirs)
        except Exception:
            pass
        sync_folder = Config.get_sync_folder()
        paths = [str(sync_folder)]
        if sync_folder.exists():
            for root, dirs, files in os.walk(str(sync_folder)):
                for d in dirs:
                    paths.append(os.path.join(root, d))
        for p in paths:
            if os.path.exists(p):
                try:
                    self._cache_watcher.addPath(p)
                except Exception:
                    pass

    def _delayed_refresh_cache(self):
        if self._cache_refresh_timer.isActive():
            self._cache_refresh_timer.stop()
        self._cache_refresh_timer.start(500)

    def _rebuild_cache_rows(self):
        """重建缓存目录行（含空目录、非房间号干扰目录，排除 preview）"""
        for row in self._cache_rows.values():
            self._cache_rows_layout.removeWidget(row)
            row.deleteLater()
        self._cache_rows = {}

        sync_folder = Config.get_sync_folder()
        dirs = []
        if sync_folder.exists():
            for d in sync_folder.iterdir():
                if d.is_dir() and d.name != "preview":
                    dirs.append(d)
        dirs.sort(key=lambda p: p.name)

        for d in dirs:
            self._add_cache_row(d)

        empty = not dirs
        if self._cache_empty_label is None:
            self._cache_empty_label = QLabel(I18n.tr('settings_cache_empty'))
            dark = _is_dark(self)
            self._cache_empty_label.setStyleSheet(
                f"font-size: 12px; color: {'#a0a0a0' if dark else '#adb5bd'};"
            )
            self._cache_empty_label.setAlignment(Qt.AlignCenter)
            self._cache_rows_layout.insertWidget(0, self._cache_empty_label)
        self._cache_empty_label.setVisible(empty)

        self._refresh_watcher()

    def _add_cache_row(self, path: Path):
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(12)
        dark = _is_dark(self)

        # 目录名使用默认调色板文字色（与创建/加入对话框字段一致）
        name_label = QLabel(path.name)
        name_label.setToolTip(str(path))
        lay.addWidget(name_label)

        lay.addStretch()

        size_label = QLabel(self._format_size(self._dir_size(path)))
        size_label.setStyleSheet(
            f"font-size: 11px; color: {'#a0a0a0' if dark else '#868e96'};"
        )
        lay.addWidget(size_label)

        clear_btn = AnimatedButton(I18n.tr('settings_clear'))
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setFixedSize(64, 30)
        clear_btn.setStyleSheet(BUTTON_STYLES['danger'])
        clear_btn.clicked.connect(lambda _, p=path: self._on_clear_dir(p))
        lay.addWidget(clear_btn)

        self._cache_rows[path.name] = row
        self._cache_rows_layout.insertWidget(self._cache_rows_layout.count() - 1, row)

    def _on_clear_dir(self, path: Path):
        """清空一个缓存目录：直接删除整个目录（非仅内容），无需二次确认"""
        try:
            safe_rmtree(str(path))
        except Exception:
            pass
        self._rebuild_cache_rows()
        self.cache_changed.emit()

    def _on_manage_folders(self):
        """打开 SyncFolder 根目录（原主界面「管理缓存」逻辑）"""
        sync_folder = Config.get_sync_folder()
        if not sync_folder.exists():
            QMessageBox.warning(self, I18n.tr('manage_cache'), I18n.tr('manage_cache_not_found'))
            return
        url = QUrl.fromLocalFile(str(sync_folder))
        if not QDesktopServices.openUrl(url):
            QMessageBox.warning(self, I18n.tr('manage_cache'), I18n.tr('manage_cache_error'))

    @staticmethod
    def _dir_size(path: Path) -> int:
        """计算单个目录占用大小（字节），失败项跳过"""
        total = 0
        try:
            for dirpath, dirnames, filenames in os.walk(str(path)):
                for fn in filenames:
                    fp = os.path.join(dirpath, fn)
                    if os.path.isfile(fp):
                        try:
                            total += os.path.getsize(fp)
                        except (OSError, PermissionError):
                            pass
        except (OSError, PermissionError):
            pass
        return total

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes == 0:
            return "0 B"
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        unit_index = 0
        size = float(size_bytes)
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        if unit_index < 2:
            return f"{int(size)} {units[unit_index]}"
        return f"{size:.2f} {units[unit_index]}"
