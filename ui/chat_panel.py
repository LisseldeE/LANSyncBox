"""
私信面板（房间内端到端私发：文本 + 文件）
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import threading
import time
import uuid

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
    QTextEdit, QSizePolicy, QGraphicsOpacityEffect, QApplication,
    QGridLayout
)
from PySide6.QtCore import (Qt, Signal, QTimer, QPropertyAnimation, QEasingCurve,
                            QRect, QRectF, QPoint, QSize, QEvent, QByteArray, Property,
                            QUrl, QMimeData)
from PySide6.QtGui import (QColor, QPainter, QPen, QPainterPath, QCursor, QPalette,
                           QMouseEvent, QFontMetrics, QDrag, QDesktopServices,
                           QTextOption, QPixmap)
from PySide6.QtSvg import QSvgRenderer

from i18n import I18n
from config import Config
from network.protocol import Protocol, MessageType
from network.file_provider import pull_file
from ui.widgets import AnimatedButton, BUTTON_STYLES


CHAT_TTL = 600        # 私信文件会话有效期（秒）：到期后条目变灰"已过期"
CHAT_PULL_MAX = 3     # 接收端拉取重连上限（复用断点续传机制）
PANEL_RATIO = 2.0 / 3.0   # 展开态占同步窗口宽度比例（PRD：2/3）
PANEL_MARGIN = 12     # 展开态上/下/右内缩
ANIM_MS = 220         # 展开/收起动画时长


def _is_dark() -> bool:
    """按窗口底色亮度判定深色主题。"""
    win = QApplication.palette().color(QApplication.palette().ColorRole.Window)
    lum = 0.299 * win.red() + 0.587 * win.green() + 0.114 * win.blue()
    return lum <= 128


def _shift(color: QColor, delta: int) -> str:
    """按明度平移出一个中性灰阶（RGB 同步偏移，不带色相）。"""
    f = lambda v: max(0, min(255, v + delta))
    return QColor(f(color.red()), f(color.green()), f(color.blue())).name()


def _palette():
    """结构色取自系统 palette（与创建/加入房间同源）；面板与手柄整体抬高一档灰度，
    与底层黑色界面区分开。"""
    pal = QApplication.palette()
    win = pal.color(QPalette.Window)
    text = pal.color(QPalette.WindowText).name()
    dark = _is_dark()
    return {
        'bg': _shift(win, 16 if dark else 12),
        'border': _shift(win, 38 if dark else -20),   # 浅灰细线：勾出手柄与卡片边缘
        'text': text,
        'muted': '#9a9a9a' if dark else '#8a8a8a',
        'row_sel': _shift(win, 30 if dark else -14),
        'mine_bg': _shift(win, 34 if dark else -18), 'mine_fg': text,
        'peer_bg': _shift(win, 2 if dark else -4), 'peer_fg': text,
    }


def _regular_files(paths) -> list:
    """只保留常规文件：投递与私信传输均为单文件流，文件夹不受支持。"""
    return [p for p in paths if p and os.path.isfile(p)]


# 文件消息卡片左侧线条图标（%C% 为描边色占位，与同步列表快捷操作栏同一套风格）
_SVG_FOLDER = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="%C%" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>'
    '</svg>')
_SVG_DOC = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="%C%" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/>'
    '<path d="M14 3v5h5"/><path d="M9 13h6M9 17h6"/></svg>')
_SVG_IMAGE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="%C%" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="16" rx="2"/>'
    '<circle cx="8.5" cy="9.5" r="1.4"/>'
    '<path d="M21 16l-5-5-6 6-3-3-4 4"/></svg>')
_SVG_ARCHIVE = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="%C%" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round">'
    '<rect x="3" y="4" width="18" height="5" rx="1"/>'
    '<path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9"/><path d="M10 13h4"/></svg>')

_EXT_IMAGE = {'png', 'jpg', 'jpeg', 'gif', 'bmp', 'webp', 'svg', 'ico', 'tif',
              'tiff', 'heic'}
_EXT_ARCHIVE = {'zip', 'rar', '7z', 'tar', 'gz', 'bz2', 'xz', 'tgz'}


def _icon_svg(name: str, path: str = '') -> str:
    """按类型挑线条图标：目录只看真实路径（可靠），其余按扩展名，未知即文档。"""
    try:
        if path and os.path.isdir(path):
            return _SVG_FOLDER
    except OSError:
        pass
    ext = os.path.splitext(name or '')[1].lstrip('.').lower()
    if ext in _EXT_IMAGE:
        return _SVG_IMAGE
    if ext in _EXT_ARCHIVE:
        return _SVG_ARCHIVE
    return _SVG_DOC


def _icon_pixmap(name: str, path: str, color: str, size: int) -> QPixmap:
    """渲染线条图标：2 倍尺寸 + devicePixelRatio，保证高分屏不糊。"""
    svg = _icon_svg(name, path).replace('%C%', color)
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    QSvgRenderer(bytearray(svg.encode('utf-8'))).render(p)
    p.end()
    pix.setDevicePixelRatio(2.0)
    return pix


class _StatusDot(QWidget):
    """端在线状态点：绿=在线、灰=断开。"""
    def __init__(self, online=True, parent=None):
        super().__init__(parent)
        self._online = online
        self.setFixedSize(10, 10)

    def set_online(self, online: bool):
        if self._online != online:
            self._online = online
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor('#51cf66' if self._online else '#9a9a9a'))
        p.drawEllipse(1, 1, 8, 8)


class _CheckMark(QWidget):
    """发送目标勾选框：纯自绘。

    面板/滚动区为透明背景设了无选择器样式，会连带作用到系统 QCheckBox，使其强调色
    丢失；自绘可稳定保证勾选态为主题蓝。
    """
    toggled = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = False
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)

    def isChecked(self) -> bool:
        return self._checked

    def setChecked(self, val: bool):
        val = bool(val)
        if self._checked != val:
            self._checked = val
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setChecked(not self._checked)
            self.toggled.emit(self._checked)
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        box = QRectF(1, 1, self.width() - 2, self.height() - 2)
        if self._checked:
            accent = QApplication.palette().color(QPalette.Highlight)
            p.setPen(Qt.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(box, 3, 3)
            p.setPen(QPen(QColor('#ffffff'), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.drawLine(4, 8, 7, 11)
            p.drawLine(7, 11, 12, 5)
        else:
            p.setPen(QPen(QColor(_palette()['border']), 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(box, 3, 3)


class _ElideLabel(QLabel):
    """按自身宽度右省略的标签：长 IP 不撑宽端列表、不挤出版面。"""
    def __init__(self, text: str = '', parent=None):
        super().__init__(parent)
        self._full = text or ''
        self.setMinimumWidth(0)
        # 横向 Ignored：sizeHint 不参与布局，宽度由行内剩余空间决定，故不会溢出
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        super().setText(self._full)

    def setText(self, text: str):
        self._full = text or ''
        self._apply()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()

    def _apply(self):
        super().setText(QFontMetrics(self.font()).elidedText(
            self._full, Qt.ElideRight, max(0, self.width())))


class _PeerRow(QFrame):
    """左栏单个端条目：状态点 + IP + 未读徽标（选择模式下带勾选框）。"""
    clicked = Signal(str)

    def __init__(self, end_id: str, ip: str, parent=None):
        super().__init__(parent)
        self.end_id = end_id
        self._selected = False
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(38)
        self._selectable = False

        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 10, 0)
        lay.setSpacing(8)
        self._check = _CheckMark()
        # 点击勾选框时事件被其自身消费，这里补发 clicked 让底部「发送」按钮刷新可用态
        self._check.toggled.connect(lambda _v: self.clicked.emit(self.end_id))
        self._check.hide()
        lay.addWidget(self._check, 0, Qt.AlignVCenter)
        self._dot = _StatusDot(True)
        lay.addWidget(self._dot)
        self._ip_label = _ElideLabel(ip or '')
        lay.addWidget(self._ip_label, 1)
        self._unread = QLabel()
        self._unread.setFixedHeight(18)
        self._unread.setMinimumWidth(18)
        self._unread.setAlignment(Qt.AlignCenter)
        self._unread.setStyleSheet(
            "background:#f03e3e; color:#ffffff; border-radius:9px; "
            "font-size:11px; padding:0 5px;")
        self._unread.hide()
        lay.addWidget(self._unread, 0, Qt.AlignVCenter)
        self._apply_style()

    def set_ip(self, ip: str):
        self._ip_label.setText(ip or '')

    def set_online(self, online: bool):
        self._dot.set_online(online)

    def set_unread(self, n: int):
        if n > 0:
            self._unread.setText(str(n) if n < 100 else '99+')
            self._unread.show()
        else:
            self._unread.hide()

    def set_selected(self, sel: bool):
        if self._selected != sel:
            self._selected = sel
            self._apply_style()

    def set_selectable(self, sel: bool, checked: bool = False):
        self._selectable = sel
        self._check.setVisible(sel)
        self._check.setChecked(checked)

    def is_checked(self) -> bool:
        return self._selectable and self._check.isChecked()

    def set_checked(self, val: bool):
        self._check.setChecked(val)

    def _apply_style(self):
        c = _palette()
        bg = c['row_sel'] if self._selected else 'transparent'
        self.setStyleSheet(
            f"QFrame {{ background:{bg}; border:none; border-radius:4px; }}"
            f"QLabel {{ color:{c['text']}; font-size:12px; background:transparent; }}")

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self._selectable:
                self._check.setChecked(not self._check.isChecked())
            self.clicked.emit(self.end_id)


class ChatHandle(QWidget):
    """收起态手柄：贴右缘的抽屉拉手（左边圆角、右边齐平）。

    闲置时收成窄条；窄条左缘刚好落在「修改时间」文字右端之外，故不遮字；鼠标
    悬浮时向左滑出，露出箭头与未读数字。
    """
    clicked = Signal()

    # 闲置宽度 = central widget 右留白(10px) + 表格 item 右 padding(6px)，即从窗口
    # 右缘到「修改时间」文字右端的全部空白（与窗口宽度无关）。取满 16px 仍不压字，
    # 同时比 10px 明显好点击；有垂直滚动条时文字再左移 14px，留白更宽，同样安全。
    IDLE_W = 16   # 闲置宽度：不越过时间文字，零遮挡
    FULL_W = 26   # 悬浮宽度：完整抽屉拉手

    def __init__(self, parent=None):
        super().__init__(parent)
        self._unread = 0
        self._expanded = False
        self._anim = None
        self.setFixedHeight(96)
        self.setMinimumWidth(self.IDLE_W)
        self.setMaximumWidth(self.FULL_W)
        self.resize(self.IDLE_W, 96)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(I18n.tr('chat_title'))

    def set_unread(self, n: int):
        n = max(0, int(n))
        if n != self._unread:
            self._unread = n
            self.update()

    def relayout(self):
        """贴右缘竖向居中；宽度按当前态取闲置/悬浮值。"""
        host = self.parentWidget()
        if host is None:
            return
        width = self.FULL_W if self._expanded else self.IDLE_W
        self.setGeometry(host.width() - width,
                         (host.height() - self.height()) // 2,
                         width, self.height())

    def enterEvent(self, event):
        self._set_expanded(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._set_expanded(False)
        super().leaveEvent(event)

    def _set_expanded(self, val: bool):
        """悬浮切换：宽度与 x 同步左移/右移，箭头与角标随宽度重绘。"""
        if self._expanded == val:
            return
        self._expanded = val
        self.update()
        host = self.parentWidget()
        if host is None or not self.isVisible():
            self.relayout()
            return
        width = self.FULL_W if val else self.IDLE_W
        y = (host.height() - self.height()) // 2
        end = QRect(host.width() - width, y, width, self.height())
        start = self.geometry()
        if start == end:
            return
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        self._anim.start(QPropertyAnimation.DeleteWhenStopped)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = _palette()
        w, h = self.width(), self.height()
        r = min(7, max(3, w // 2))   # 窄条用小圆角，避免糊成半圆
        # 抽屉拉手外形：左边圆角、右边与窗口边缘齐平
        shape = QPainterPath()
        shape.moveTo(w - 0.5, 0.5)
        shape.lineTo(r, 0.5)
        shape.quadTo(0.5, 0.5, 0.5, r)
        shape.lineTo(0.5, h - r)
        shape.quadTo(0.5, h - 0.5, r, h - 0.5)
        shape.lineTo(w - 0.5, h - 0.5)
        p.setBrush(QColor(c['bg']))
        p.setPen(QPen(QColor(c['border']), 1))
        p.drawPath(shape)

        wide = w >= self.FULL_W - 2
        # 左向箭头（纯线条，中性色）：仅完整态有位置画
        if wide:
            p.setPen(QPen(QColor(c['muted']), 1.4, Qt.SolidLine, Qt.RoundCap))
            ax, ay = w // 2 + 2, h // 2
            p.drawLine(ax + 3, ay - 4, ax - 1, ay)
            p.drawLine(ax - 1, ay, ax + 3, ay + 4)

        # 未读角标：完整态显示数字，窄条态缩成小圆点；两者均贴顶、水平居中
        if self._unread > 0:
            d = 15 if wide else 7
            cx, cy = (w - d) / 2, 4
            p.setPen(Qt.NoPen)
            p.setBrush(QColor('#f03e3e'))
            p.drawEllipse(QRectF(cx, cy, d, d))
            if wide:
                txt = str(self._unread) if self._unread < 100 else '99+'
                p.setPen(QColor('#ffffff'))
                f = p.font()
                f.setPointSize(7)
                p.setFont(f)
                p.drawText(QRectF(cx, cy, d, d), Qt.AlignCenter, txt)


class _ChatInput(QTextEdit):
    """聊天输入框：沿用创建房间「下划线输入框」的观感——无外框、透明底，仅底部一条
    细线，聚焦时高亮线自中间向两侧展开；高度随输入内容自适应（上/下限内）。

    之所以不直接用 QLineEdit，是因为聊天需要多行（Shift+回车换行）。
    """

    MIN_H = 40        # 空内容时的最小高度
    MAX_H = 140       # 超出后不再增高，改为内部滚动
    ANIM_MS = 260     # 下划线展开/收起动画时长

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # 拖入的是文件时不应插入路径文本，让拖拽事件冒泡到面板按"发送文件"处理
        self.setAcceptDrops(False)
        self.setStyleSheet(
            "QTextEdit { background: transparent; border: none; color: palette(text);"
            " selection-background-color: palette(highlight);"
            " selection-color: palette(highlighted-text); }")
        self.document().setDocumentMargin(6)   # 四周留白；底部亦为下划线预留空间
        self._progress = 0.0
        self._anim = QPropertyAnimation(self, QByteArray(b"underlineProgress"), self)
        self._anim.setDuration(self.ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.document().documentLayout().documentSizeChanged.connect(self._fit_height)
        self._fit_height()

    # ------------------------------------------------ 动画进度属性
    def get_underline_progress(self) -> float:
        return self._progress

    def set_underline_progress(self, value: float):
        self._progress = value
        self.viewport().update()   # 下划线画在 viewport 上，须由其重绘触发

    underlineProgress = Property(float, get_underline_progress, set_underline_progress)

    # ------------------------------------------------ 高度自适应
    def _fit_height(self, *_):
        # 显式同步文本宽度，保证文档换行/高度在未完成首次布局时也可算出
        vw = self.viewport().width()
        if vw > 0 and self.document().textWidth() != vw:
            self.document().setTextWidth(vw)
        doc_h = self.document().size().height()
        h = max(self.MIN_H, min(self.MAX_H, int(doc_h) + 8))
        if h != self.height():
            self.setFixedHeight(h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    # ------------------------------------------------ 焦点/悬浮
    def focusInEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(1.0)
        self._anim.start()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._anim.stop()
        self._anim.setStartValue(self._progress)
        self._anim.setEndValue(0.0)
        self._anim.start()
        super().focusOutEvent(event)

    # ------------------------------------------------ 绘制
    def paintEvent(self, event):
        super().paintEvent(event)
        # QTextEdit 是 QAbstractScrollArea：控件自身无 paintEngine，绘制必须落在 viewport 上
        vp = self.viewport()
        p = QPainter(vp)
        p.setRenderHint(QPainter.Antialiasing, True)
        pal = self.palette()
        line_y = vp.height() - 2
        # 常态底线
        p.setPen(QPen(QColor(_palette()['border']), 1.0))   # 深色模式取更亮灰阶，否则灰线不可见
        p.drawLine(0, line_y, vp.width(), line_y)
        # 聚焦高亮线：从中间向两侧展开
        if self._progress > 0:
            half = int(vp.width() / 2.0 * self._progress)
            p.setPen(QPen(pal.color(QPalette.Highlight), 2.0))
            p.drawLine(vp.width() // 2 - half, line_y,
                       vp.width() // 2 + half + 1, line_y)
        p.end()


class _BubbleLabel(QWidget):
    """消息气泡：按可用宽度自动换行，长串无空格（URL/长数字）也随处断行。

    不用 QLabel——其换行只在词边界，超宽长串会挤成一行被裁掉；自绘可让高度与文本
    实际布局严格一致。
    """

    PAD_X, PAD_Y = 10, 6   # 内边距（原 QSS padding:6px 10px）

    def __init__(self, text: str, bg: str, fg: str, parent=None):
        super().__init__(parent)
        self._text = text or ''
        self._bg = QColor(bg)
        self._fg = QColor(fg)
        self._max_w = 360
        f = self.font()
        f.setPixelSize(12)
        self.setFont(f)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)

    def setMaximumWidth(self, width: int):
        self._max_w = int(width)
        super().setMaximumWidth(int(width))

    def _measure(self, avail: int):
        r = QFontMetrics(self.font()).boundingRect(
            QRect(0, 0, max(1, avail - 2 * self.PAD_X), 100000),
            int(Qt.AlignLeft | Qt.TextWrapAnywhere), self._text)
        return r.width() + 2 * self.PAD_X, r.height() + 2 * self.PAD_Y

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._measure(width)[1]

    def sizeHint(self):
        w, h = self._measure(self._max_w)
        return QSize(w, h)

    def minimumSizeHint(self):
        return self.sizeHint()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._bg)
        p.drawRoundedRect(QRectF(self.rect()), 6, 6)
        opt = QTextOption()
        opt.setAlignment(Qt.AlignLeft)
        opt.setWrapMode(QTextOption.WrapAnywhere)
        p.setPen(self._fg)
        p.drawText(QRectF(self.PAD_X, self.PAD_Y,
                          self.width() - 2 * self.PAD_X,
                          self.height() - 2 * self.PAD_Y), self._text, opt)


class _FileCard(QFrame):
    """文件消息卡片：固定长方形；双击用系统默认应用打开，按住拖出即为"另存为"
    （与同步列表一致：拖拽只做复制，不移动源文件）。

    卡片内的文字标签默认忽略鼠标事件，事件会自然冒泡到卡片；右侧「接收」按钮
    自行处理点击，不会触发打开/拖出。
    """

    def __init__(self, panel, item: dict, parent=None):
        super().__init__(parent)
        self._panel = panel
        self._item = item
        self._press_pos = None
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._press_pos = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._press_pos is None or not (event.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(event)
        moved = (event.pos() - self._press_pos).manhattanLength()
        if moved < QApplication.startDragDistance():
            return super().mouseMoveEvent(event)
        self._press_pos = None
        self._panel.start_drag_file(self._item)

    def mouseReleaseEvent(self, event):
        self._press_pos = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._panel.open_file(self._item)
            return
        super().mouseDoubleClickEvent(event)


class ChatPanel(QWidget):
    """私信浮层：左侧端列表 + 右侧聊天区，覆盖于同步界面之上（不挤压底层）。"""

    # 字节数用 64 位整型（'qlonglong'）：>2GB 文件在 32 位 int 信号中溢出为负数，
    # 表现为接收进度瞬间越过 100% 后一路负增长（投递进度条曾踩过同一坑）
    _pull_progress = Signal(str, 'qlonglong', 'qlonglong')  # item_key, recv, total
    _pull_done = Signal(str, bool, str, str)  # item_key, ok, err, path
    # 接收日志行收尾（含取消）：recv_log_key, name, ok
    _recv_log_end = Signal(str, str, bool)

    FILE_CARD_W = 300    # 文件消息卡片固定宽度
    FILE_CARD_H = 64     # 文件消息卡片固定高度
    RECV_BTN_W = 64      # 卡片右侧「接收」按钮固定宽度
    ICON_W = 24          # 卡片左侧文件类型图标边长

    def __init__(self, owner, parent=None):
        super().__init__(parent or owner)
        self._owner = owner
        self._room_code = getattr(owner, 'room_code', '') or ''
        self._peers = {}       # end_id -> {'ip','online','unread'}
        self._rows = {}        # end_id -> _PeerRow
        self._history = {}     # end_id -> [item dict]
        self._items = {}       # item_key -> item dict
        self._out_sessions = {}  # session_id -> end_id（本端发起的私信文件会话）
        self._current = None   # 当前选中的端
        self._expanded = False
        self._dispatch_files = None
        self._dispatch_mode = 'none'   # none / select / direct
        self._anim = None
        self._outside_filter = False   # 展开期间是否已挂全局点击过滤器

        self.setObjectName('ChatPanel')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setAcceptDrops(True)   # 面板内任意位置拖入文件即按"发送文件"处理
        self._build_ui()
        self.setVisible(False)

        self.handle = ChatHandle(self.parentWidget() or owner)
        self.handle.clicked.connect(self.expand)
        self.handle.show()
        self.handle.raise_()

        self._pull_progress.connect(self._on_pull_progress)
        self._pull_done.connect(self._on_pull_done)
        self._recv_log_end.connect(self._on_recv_log_end)

        # 会话有效期扫描：到期条目变灰"已过期"
        self._ttl_timer = QTimer(self)
        self._ttl_timer.setInterval(10000)
        self._ttl_timer.timeout.connect(self._sweep_expired)
        self._ttl_timer.start()

    # ---- 构建界面 ----

    def _build_ui(self):
        c = _palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 12)
        root.setSpacing(8)
        self.setStyleSheet(
            f"#ChatPanel {{ background:{c['bg']}; border:1px solid {c['border']}; "
            f"border-radius:8px; }}")

        # 顶部标题行
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel(I18n.tr('chat_title'))
        title.setStyleSheet(
            f"color:{c['text']}; font-size:13px; font-weight:bold; background:transparent;")
        head.addWidget(title)
        head.addStretch()
        self._read_all_btn = AnimatedButton(I18n.tr('chat_mark_all_read'))
        self._read_all_btn.clicked.connect(self._mark_all_read)
        head.addWidget(self._read_all_btn)
        self._collapse_btn = AnimatedButton(I18n.tr('collapse_panel'))
        self._collapse_btn.clicked.connect(lambda: self.collapse())
        head.addWidget(self._collapse_btn)
        root.addLayout(head)

        # 主体：左栏端列表 + 右栏聊天区
        body = QHBoxLayout()
        body.setSpacing(8)

        # 左栏（无独立卡片，仅与右栏以 1px 分隔线区分）
        left = QFrame()
        left.setFixedWidth(160)
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(4, 6, 4, 6)
        left_lay.setSpacing(2)
        self._peer_scroll = QScrollArea()
        self._peer_scroll.setWidgetResizable(True)
        self._peer_scroll.setFrameShape(QFrame.NoFrame)
        # 端列表只做纵向滚动：横向滚动条是布局溢出的补丁，不该出现
        self._peer_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._peer_scroll.setStyleSheet("background:transparent; border:none;")
        self._peer_host = QWidget()
        self._peer_list = QVBoxLayout(self._peer_host)
        self._peer_list.setContentsMargins(0, 0, 0, 0)
        self._peer_list.setSpacing(2)
        self._peer_list.addStretch()
        self._peer_scroll.setWidget(self._peer_host)
        left_lay.addWidget(self._peer_scroll, 1)
        body.addWidget(left)

        # 左右分隔线
        vline = QFrame()
        vline.setFixedWidth(1)
        vline.setStyleSheet(f"background:{c['border']}; border:none;")
        body.addWidget(vline)

        # 右栏
        right = QFrame()
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(8, 8, 8, 8)
        right_lay.setSpacing(6)

        self._chat_title = QLabel('')
        self._chat_title.setStyleSheet(
            f"color:{c['muted']}; font-size:12px; background:transparent;")
        right_lay.addWidget(self._chat_title)

        # 未选中端时居中显示的引导语
        self._chat_hint = QLabel(I18n.tr('chat_select_hint'))
        self._chat_hint.setAlignment(Qt.AlignCenter)
        self._chat_hint.setStyleSheet(
            f"color:{c['muted']}; font-size:13px; background:transparent;")
        right_lay.addWidget(self._chat_hint, 1)

        # 消息滚动区（消息贴底、向上生长）
        self._msg_scroll = QScrollArea()
        self._msg_scroll.setWidgetResizable(True)
        self._msg_scroll.setFrameShape(QFrame.NoFrame)
        self._msg_scroll.setStyleSheet("background:transparent; border:none;")
        self._msg_host = QWidget()
        self._msg_list = QVBoxLayout(self._msg_host)
        self._msg_list.setContentsMargins(0, 0, 0, 0)
        self._msg_list.setSpacing(6)
        self._msg_list.addStretch()   # 索引 0：撑开顶部，消息向下堆叠
        self._msg_scroll.setWidget(self._msg_host)
        right_lay.addWidget(self._msg_scroll, 1)

        # 底部操作区（无选中端且未投递时整体隐藏）
        self._input_area = QWidget()
        ia_lay = QVBoxLayout(self._input_area)
        ia_lay.setContentsMargins(0, 0, 0, 0)
        ia_lay.setSpacing(6)

        self._dispatch_info = QLabel()
        self._dispatch_info.setWordWrap(True)
        self._dispatch_info.setStyleSheet(
            f"color:{c['text']}; font-size:12px; background:transparent;")
        self._dispatch_info.hide()
        ia_lay.addWidget(self._dispatch_info)

        # 输入框与发送按钮同行：输入框在左拉伸，按钮贴右下对齐下划线
        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        # 与创建房间的密码框同款「下划线」观感，高度随内容自适应
        self._input = _ChatInput()
        self._input.setPlaceholderText(I18n.tr('chat_input_hint'))
        self._input.installEventFilter(self)
        self._input.textChanged.connect(self._update_send_btn_state)
        input_row.addWidget(self._input, 1)
        self._cancel_btn = AnimatedButton(I18n.tr('cancel'))
        self._cancel_btn.setStyleSheet(BUTTON_STYLES['secondary'])
        self._cancel_btn.clicked.connect(self._on_cancel_dispatch)
        self._cancel_btn.hide()
        input_row.addWidget(self._cancel_btn, 0, Qt.AlignBottom)
        self._send_btn = AnimatedButton(I18n.tr('chat_send'))
        self._send_btn.setStyleSheet(BUTTON_STYLES['primary'])
        self._send_btn.clicked.connect(self._on_send_clicked)
        self._send_btn.setEnabled(False)
        input_row.addWidget(self._send_btn, 0, Qt.AlignBottom)
        ia_lay.addLayout(input_row)

        self._input_area.setVisible(False)
        right_lay.addWidget(self._input_area)

        # 视口与内容层保持透明，避免平台 base 色渗进面板
        for area, host in ((self._peer_scroll, self._peer_host),
                           (self._msg_scroll, self._msg_host)):
            area.viewport().setStyleSheet("background:transparent;")
            host.setStyleSheet("background:transparent;")

        body.addWidget(right, 1)
        root.addLayout(body, 1)

        self._update_view_state()

    # ---- 定位与展开/收起 ----

    def _host_size(self):
        """定位基准：面板父控件（central widget）尺寸；无父时退回同步窗口。"""
        host = self.parentWidget() or self._owner
        return host.width(), host.height()

    def relayout(self):
        """窗口尺寸变化时重定位：手柄贴右缘竖向居中，面板贴右侧内缩。"""
        w, h = self._host_size()
        if self.handle is not None:
            self.handle.relayout()
        pw = int(w * PANEL_RATIO)
        ph = h - PANEL_MARGIN * 2
        x = w - pw - PANEL_MARGIN
        if self._expanded:
            self.setGeometry(x, PANEL_MARGIN, pw, ph)
        else:
            self.setGeometry(w, PANEL_MARGIN, pw, ph)

    def is_expanded(self) -> bool:
        return self._expanded

    def expand(self, animate: bool = True):
        """展开面板：从右缘向左推出。"""
        self.relayout()
        w, h = self._host_size()
        pw = int(w * PANEL_RATIO)
        end = QRect(w - pw - PANEL_MARGIN, PANEL_MARGIN, pw, h - PANEL_MARGIN * 2)
        start = QRect(w, end.y(), end.width(), end.height())
        self.show()
        self.raise_()
        self._expanded = True
        self.handle.hide()
        self._sync_peers()
        self._update_view_state()
        self._install_outside_filter()
        if animate:
            self._animate_geometry(start, end)
        else:
            self.setGeometry(end)

    def collapse(self, animate: bool = True):
        """收起面板：向右滑出并隐藏，露出小手柄。"""
        self._end_dispatch()
        w, _h = self._host_size()
        start = self.geometry()
        end = QRect(w, start.y(), start.width(), start.height())
        self._expanded = False
        self._remove_outside_filter()
        if animate:
            self._animate_geometry(start, end, on_end=self._after_collapse)
        else:
            self._after_collapse()

    def _after_collapse(self):
        self.setVisible(False)
        self.handle.show()
        self.handle.raise_()
        self.relayout()

    def _install_outside_filter(self):
        """展开期间挂应用级过滤器：点击面板之外即收起。"""
        if self._outside_filter:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._outside_filter = True

    def _remove_outside_filter(self):
        """收起后摘掉过滤器，避免常态下过滤全应用事件。"""
        if not self._outside_filter:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._outside_filter = False

    def _outside_press(self, obj, event) -> bool:
        """判断本次鼠标按下是否落在面板/手柄之外（同一窗口内）。"""
        if not isinstance(obj, QWidget):
            return False
        if obj is self.handle or self.handle.isAncestorOf(obj):
            return False
        if obj is self or self.isAncestorOf(obj):
            return False
        if obj.window() is not self.window():
            return False   # 独立弹窗内的点击不收起
        return not self.rect().contains(self.mapFromGlobal(
            event.globalPosition().toPoint()))

    def _animate_geometry(self, start: QRect, end: QRect, on_end=None):
        self._anim = QPropertyAnimation(self, b"geometry")
        self._anim.setDuration(ANIM_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._anim.setStartValue(start)
        self._anim.setEndValue(end)
        if on_end is not None:
            self._anim.finished.connect(on_end)
        self._anim.start(QPropertyAnimation.DeleteWhenStopped)

    # ---- 端列表 ----

    def _mesh(self):
        try:
            return self._owner._mesh_of()
        except Exception:
            return None

    def _local_end_id(self) -> str:
        m = self._mesh()
        return getattr(m, 'end_id', '') if m else ''

    def _provider(self):
        return getattr(self._owner, '_provider', None)

    def _peer_ip(self, end_id: str) -> str:
        m = self._mesh()
        if m is None:
            return ''
        ep = m.get_peer(end_id)
        return getattr(ep, 'ip', '') if ep is not None else ''

    def _ensure_peer(self, end_id: str) -> dict:
        entry = self._peers.get(end_id)
        if entry is None:
            entry = {'ip': self._peer_ip(end_id) or I18n.tr('ip_unknown'),
                     'online': False, 'unread': 0}
            self._peers[end_id] = entry
        return entry

    def _sync_peers(self):
        """从 mesh 活跃直连快照刷新在线端（保留有历史记录的离线端）。"""
        m = self._mesh()
        online = set(m.connected_end_ids()) if m else set()
        online.discard(self._local_end_id())
        for end_id in online:
            entry = self._ensure_peer(end_id)
            ip = self._peer_ip(end_id)
            if ip:
                entry['ip'] = ip
            entry['online'] = True
        for end_id, entry in self._peers.items():
            if end_id not in online:
                entry['online'] = False
        self._refresh_peer_rows()

    def _refresh_peer_rows(self):
        want = set(self._peers.keys())
        # 移除多余行
        for end_id in list(self._rows.keys()):
            if end_id not in want:
                row = self._rows.pop(end_id)
                self._peer_list.removeWidget(row)
                row.deleteLater()
        # 新增/更新行
        for end_id, entry in self._peers.items():
            row = self._rows.get(end_id)
            if row is None:
                row = _PeerRow(end_id, entry['ip'])
                row.clicked.connect(self._on_peer_row_clicked)
                self._rows[end_id] = row
                self._peer_list.insertWidget(0, row)
            else:
                row.set_ip(entry['ip'])
            row.set_online(entry['online'])
            row.set_selected(end_id == self._current)
            row.set_unread(0 if end_id == self._current else entry['unread'])
            if self._dispatch_mode == 'select':
                row.set_selectable(True, row.is_checked())
            else:
                row.set_selectable(False)
        self._refresh_handle_badge()
        self._update_send_btn_state()

    def on_peer_connected(self, end_id: str, name: str = ''):
        if not end_id or end_id == self._local_end_id():
            return
        entry = self._ensure_peer(end_id)
        ip = self._peer_ip(end_id)
        if ip:
            entry['ip'] = ip
        entry['online'] = True
        self._refresh_peer_rows()

    def on_peer_disconnected(self, end_id: str):
        entry = self._peers.get(end_id)
        if entry is None:
            return
        entry['online'] = False
        self._expire_peer_items(end_id)
        self._refresh_peer_rows()

    def _select_peer(self, end_id: str):
        if end_id not in self._peers:
            return
        self._current = end_id
        self._peers[end_id]['unread'] = 0
        self._chat_title.setText(self._peers[end_id]['ip'])
        self._refresh_peer_rows()
        self._render_history(end_id)
        self._update_view_state()
        self._update_send_btn_state()

    def _mark_all_read(self):
        """全部已读：清空所有端未读，刷新列表徽标与收起点角标。"""
        for entry in self._peers.values():
            entry['unread'] = 0
        self._refresh_peer_rows()

    def _on_peer_row_clicked(self, end_id: str):
        if self._dispatch_mode == 'select':
            self._update_send_btn_state()
            return
        self._select_peer(end_id)

    def _refresh_handle_badge(self):
        # 展开且已选中某端时，该端未读不计入手柄角标（用户正看着它）
        total = sum(e['unread'] for eid, e in self._peers.items()
                    if not (self._expanded and eid == self._current))
        self.handle.set_unread(total)

    def _bump_unread(self, end_id: str):
        if end_id == self._current and self._expanded:
            return
        entry = self._ensure_peer(end_id)
        entry['unread'] = entry.get('unread', 0) + 1
        row = self._rows.get(end_id)
        if row is not None:
            row.set_unread(entry['unread'])
        self._refresh_handle_badge()

    # ---- 消息渲染 ----

    def _chat_dir(self, end_id: str) -> str:
        """接收文件落盘目录：preview/.chat/<房间号>/<end_id>/（退出即随 preview 清空）。"""
        base = Config.get_preview_folder() / '.chat' / self._room_code / end_id
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return str(base)

    def _render_history(self, end_id: str):
        """清空并重建某端的消息（切端时调用）。"""
        while self._msg_list.count() > 1:
            item = self._msg_list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        for item in self._history.get(end_id, []):
            widget = self._build_item_widget(end_id, item)
            item['widget'] = widget
            self._msg_list.insertWidget(self._msg_list.count() - 1, widget)
        self._scroll_to_bottom(animate=False)

    def _append_item(self, end_id: str, item: dict):
        self._history.setdefault(end_id, []).append(item)
        if end_id == self._current:
            widget = self._build_item_widget(end_id, item)
            item['widget'] = widget
            self._msg_list.insertWidget(self._msg_list.count() - 1, widget)
            self._fade_in(widget)
            self._scroll_to_bottom(animate=True)

    def _build_item_widget(self, end_id: str, item: dict) -> QWidget:
        if item.get('kind') == 'text':
            return self._build_text_bubble(item)
        return self._build_file_bubble(end_id, item)

    def _build_text_bubble(self, item: dict) -> QWidget:
        c = _palette()
        mine = item.get('mine')
        row = QWidget()
        # 垂直 Maximum：行高贴合气泡实际高度，不被消息区剩余空间拉长
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(0, 0, 0, 0)
        bubble = _BubbleLabel(item.get('text', ''),
                              c['mine_bg'] if mine else c['peer_bg'],
                              c['mine_fg'] if mine else c['peer_fg'])
        bubble.setMaximumWidth(self.FILE_CARD_W)   # 上限与文件消息卡片同宽
        if mine:
            lay.addStretch()
            lay.addWidget(bubble, 0, Qt.AlignVCenter)
        else:
            lay.addWidget(bubble, 0, Qt.AlignVCenter)
            lay.addStretch()
        return row

    def _build_file_bubble(self, end_id: str, item: dict) -> QWidget:
        """文件消息：固定长方形卡片；接收按钮置于卡片右侧（非下方），用全局蓝色。"""
        c = _palette()
        mine = item.get('mine')
        card = _FileCard(self, item)
        card.setFixedSize(self.FILE_CARD_W, self.FILE_CARD_H)
        card.setStyleSheet(
            f"QFrame {{ background:{c['peer_bg']}; border:none; "
            f"border-radius:6px; }}")
        lay = QGridLayout(card)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setHorizontalSpacing(8)
        lay.setVerticalSpacing(4)

        full = item.get('name', '')

        # 左侧文件类型线条图标（按真实目录/扩展名区分）
        icon = QLabel()
        icon.setFixedSize(self.ICON_W, self.ICON_W)
        icon.setPixmap(_icon_pixmap(full, item.get('path', ''), c['text'], self.ICON_W))
        icon.setStyleSheet("background:transparent; border:none;")
        lay.addWidget(icon, 0, 0, 2, 1, Qt.AlignVCenter)

        # 名称：固定宽度 + 中间省略，保证卡片形状/大小恒定不随文件名伸缩
        name_w = self._file_text_width(not mine)
        name = QLabel()
        nf = name.font()
        nf.setPixelSize(12)
        name.setFont(nf)
        name.setText(QFontMetrics(nf).elidedText(full, Qt.ElideMiddle, name_w))
        name.setToolTip(full)
        name.setFixedWidth(name_w)
        name.setStyleSheet(
            f"color:{c['text']}; background:transparent; border:none;")
        item['name_label'] = name
        item['name_full'] = full
        lay.addWidget(name, 0, 1)

        # 次行：大小 · 状态
        meta = QLabel()
        mf = meta.font()
        mf.setPixelSize(11)
        meta.setFont(mf)
        meta.setStyleSheet(
            f"color:{c['muted']}; background:transparent; border:none;")
        lay.addWidget(meta, 1, 1)
        item['status_label'] = meta
        item['size_text'] = self._fmt_size(item.get('size', 0))

        if not mine:
            recv = AnimatedButton(I18n.tr('chat_receive'))
            recv.setStyleSheet(BUTTON_STYLES['primary'])
            recv.setFixedWidth(self.RECV_BTN_W)
            recv.clicked.connect(lambda: self._start_pull(item))
            lay.addWidget(recv, 0, 2, 2, 1, Qt.AlignVCenter)
            item['recv_btn'] = recv

        self._update_file_item(item)
        # 外层行包一层，控制左右对齐
        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        if mine:
            rl.addStretch()
            rl.addWidget(card)
        else:
            rl.addWidget(card)
            rl.addStretch()
        item['row_widget'] = row
        return row

    def _file_text_width(self, with_btn: bool) -> int:
        """卡片内文字可用宽度：扣掉左右内边距、左图标与其右侧间距，以及「接收」按钮占位。"""
        gap = self.ICON_W + 8
        return (self.FILE_CARD_W - 24 - gap
                - (self.RECV_BTN_W + 8 if with_btn else 0))

    @staticmethod
    def _fmt_size(n) -> str:
        try:
            n = float(n)
        except (TypeError, ValueError):
            return ''
        for unit in ('B', 'KB', 'MB', 'GB'):
            if n < 1024 or unit == 'GB':
                return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
            n /= 1024
        return ''

    def _update_file_item(self, item: dict):
        label = item.get('status_label')
        if label is None:
            return
        state = item.get('state', 'pending')
        key = {'pending': 'chat_file_pending', 'downloading': 'chat_file_downloading',
               'sent': 'chat_file_sent', 'done': 'chat_file_done',
               'failed': 'chat_file_failed',
               'expired': 'chat_file_expired'}.get(state, 'chat_file_pending')
        if state == 'downloading' and item.get('total'):
            pct = max(0, min(100, int(item.get('recv', 0) * 100 / max(1, item['total']))))
            status = f"{I18n.tr('chat_file_downloading')} {pct}%"
        else:
            status = I18n.tr(key)
        size_text = item.get('size_text') or self._fmt_size(item.get('size', 0))
        label.setText(f"{size_text} · {status}" if size_text else status)
        btn = item.get('recv_btn')
        show_btn = state in ('pending', 'failed')
        if btn is not None:
            btn.setVisible(show_btn)
        # 按钮隐藏后栅格列坍缩，定宽标签会被居中而在左侧留空档；宽度随按钮显隐同步，
        # 释放出来的宽度回补给文字（同时重新省略，避免省略号位置与实际宽度不符）
        name = item.get('name_label')
        if name is not None:
            w = self._file_text_width(btn is not None and show_btn)
            name.setFixedWidth(w)
            name.setText(QFontMetrics(name.font()).elidedText(
                item.get('name_full', ''), Qt.ElideMiddle, w))
            label.setFixedWidth(w)
        if state in ('sent', 'done', 'failed', 'expired'):
            item['expire_at'] = 0   # 终态不再参与过期扫描

    def _fade_in(self, widget: QWidget):
        try:
            eff = QGraphicsOpacityEffect(widget)
            widget.setGraphicsEffect(eff)
            anim = QPropertyAnimation(eff, b"opacity", widget)
            anim.setDuration(180)
            anim.setStartValue(0.0)
            anim.setEndValue(1.0)
            anim.setEasingCurve(QEasingCurve.OutCubic)
            anim.finished.connect(lambda: widget.setGraphicsEffect(None))
            anim.start(QPropertyAnimation.DeleteWhenStopped)
        except Exception:
            pass

    def _scroll_to_bottom(self, animate: bool = True):
        bar = self._msg_scroll.verticalScrollBar()
        target = bar.maximum()
        if not animate:
            bar.setValue(target)
            return
        anim = QPropertyAnimation(bar, b"value", self)
        anim.setDuration(200)
        anim.setStartValue(bar.value())
        anim.setEndValue(target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    # ---- 收发 ----

    def on_chat_message(self, from_id: str, msg_type: int, content: dict):
        """收到私信帧（mesh 直连 → UI 线程）：文本 / 文件会话 / 会话失效。"""
        if not isinstance(content, dict):
            return
        if msg_type == MessageType.CHAT_TEXT:
            self._ensure_peer(from_id)
            self._append_item(from_id, {'kind': 'text', 'mine': False,
                                        'text': content.get('text', ''),
                                        'ts': content.get('ts', time.time())})
            self._bump_unread(from_id)
        elif msg_type == MessageType.CHAT_FILE_OFFER:
            self._ensure_peer(from_id)
            sids = content.get('session_id', '')
            token = content.get('token', '')
            host = content.get('host', '')
            port = int(content.get('port', 0) or 0)
            ttl = int(content.get('ttl', 0) or CHAT_TTL)
            for f in content.get('files') or []:
                name = os.path.basename(f.get('name', '')) or f.get('name', '')
                if not name:
                    continue
                key = f"{from_id}\x1f{sids}\x1f{name}"
                if key in self._items:
                    continue
                item = {'kind': 'file', 'mine': False, 'key': key,
                        'name': name, 'size': f.get('size', 0),
                        'session_id': sids, 'token': token, 'host': host,
                        'port': port, 'state': 'pending', 'recv': 0, 'total': 0,
                        'end_id': from_id, 'stop_event': threading.Event(),
                        'expire_at': time.time() + ttl}
                self._items[key] = item
                self._append_item(from_id, item)
            self._bump_unread(from_id)
        elif msg_type == MessageType.CHAT_SESSION_CLOSE:
            self._expire_session(from_id, content.get('session_id', ''))

    def _send_text(self):
        text = self._input.toPlainText().strip()
        end_id = self._current
        if not text or not end_id:
            return
        mesh = self._mesh()
        data = Protocol.create_chat_text(self._local_end_id(), uuid.uuid4().hex, text)
        ok = bool(mesh and mesh.send_to_peer(end_id, data))
        if not ok:
            self._toast(I18n.tr('chat_send_fail'))
        self._append_item(end_id, {'kind': 'text', 'mine': True, 'text': text,
                                   'ts': time.time()})
        self._input.clear()

    def _on_send_clicked(self):
        if self._dispatch_mode != 'none':
            self._on_dispatch_send()
            return
        self._send_text()

    def eventFilter(self, obj, event):
        if obj is self._input and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter) \
                    and not (event.modifiers() & Qt.ShiftModifier):
                if self._dispatch_mode == 'none':
                    self._send_text()
                    return True
        elif self._expanded and isinstance(event, QMouseEvent) \
                and event.type() == QEvent.MouseButtonPress \
                and self._outside_press(obj, event):
            self.collapse()
        return super().eventFilter(obj, event)

    # ---- 文件发送（拖拽投递） ----

    def handle_drop_files(self, files: list):
        """拖至列表右侧 1/3 松手：展开面板并进入"目标选择"投递流程。

        此入口固定走目标选择：左栏端列表带勾选框 + 底部「发送/取消」按钮，
        右栏给提示语（不展示聊天界面）。
        """
        files = [f for f in files if f]
        if not files:
            return
        self.expand()
        self._start_dispatch(files, 'select')

    def _start_dispatch(self, files: list, mode: str):
        """进入投递态：mode='select' 手动勾选目标；mode='direct' 直发当前会话端。

        投递与私信都是单文件流，文件夹不支持：统一在此漏斗剔除并提示，
        只剩文件夹则不进投递态。
        """
        good = _regular_files(files)
        if len(good) < len(files):
            self._toast(I18n.tr('chat_folder_unsupported'))
        if not good:
            return
        files = good
        self._dispatch_files = list(files)
        self._dispatch_mode = mode
        names = [os.path.basename(f) for f in files]
        info = (names[0] if len(names) == 1
                else I18n.tr('chat_file_count', count=len(names)))
        self._dispatch_info.setText(info)
        self._dispatch_info.show()
        self._cancel_btn.show()
        self._send_btn.setText(I18n.tr('chat_send'))
        self._update_view_state()
        self._refresh_peer_rows()

    @staticmethod
    def _local_files(mime) -> list:
        """从拖拽数据中取本地文件路径（非文件拖拽返回空列表）。"""
        if mime is None or not mime.hasUrls():
            return []
        return [u.toLocalFile() for u in mime.urls() if u.toLocalFile()]

    def _is_internal_drag(self, event) -> bool:
        """本面板自身发起的拖拽（文件消息拖出另存）不算"拖入文件"，须放行不拦截。"""
        src = event.source()
        return src is not None and (src is self or self.isAncestorOf(src))

    def dragEnterEvent(self, event):
        # 只认常规文件：纯文件夹拖入直接拒绝（光标显示禁止），不进入投递态
        if not self._is_internal_drag(event) and _regular_files(self._local_files(event.mimeData())):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if not self._is_internal_drag(event) and _regular_files(self._local_files(event.mimeData())):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        """聊天界面内拖入文件：直接作为文件消息发给当前会话端。"""
        if self._is_internal_drag(event):
            event.ignore()
            return
        files = self._local_files(event.mimeData())
        if not files:
            event.ignore()
            return
        event.acceptProposedAction()
        self._start_dispatch(files, 'direct' if self._current else 'select')

    def _dispatch_targets(self) -> list:
        if self._dispatch_mode == 'direct':
            return [self._current] if self._current else []
        return [eid for eid, row in self._rows.items() if row.is_checked()]

    def _update_view_state(self):
        """按选中端/投递态切换右栏视图：目标选择投递时右栏只给引导语（不展示聊天），
        直发/聊天时展示会话；投递期间输入框让位给文件信息与发送按钮。"""
        has_peer = self._current is not None
        mode = self._dispatch_mode
        show_chat = has_peer and mode in ('none', 'direct')
        self._input_area.setVisible(mode != 'none' or has_peer)
        self._input.setVisible(mode == 'none')
        self._msg_scroll.setVisible(show_chat)
        self._chat_title.setVisible(show_chat)
        self._chat_hint.setVisible(not show_chat)
        self._chat_hint.setText(I18n.tr('chat_targets') if mode == 'select'
                                else I18n.tr('chat_select_hint'))

    def _update_send_btn_state(self):
        """投递态按已选目标数控灰；聊天态按是否有选中端且输入非空控灰。"""
        if self._dispatch_mode != 'none':
            self._send_btn.setEnabled(bool(self._dispatch_targets()))
        else:
            self._send_btn.setEnabled(
                self._current is not None
                and bool(self._input.toPlainText().strip()))

    def _on_dispatch_send(self):
        targets = self._dispatch_targets()
        if not targets:
            return
        self._send_files(self._dispatch_files or [], targets)
        self._end_dispatch()

    def _on_cancel_dispatch(self):
        self._end_dispatch()

    def _end_dispatch(self):
        self._dispatch_mode = 'none'
        self._dispatch_files = None
        self._dispatch_info.hide()
        self._cancel_btn.hide()
        self._send_btn.setText(I18n.tr('chat_send'))
        self._update_view_state()
        self._update_send_btn_state()
        for row in self._rows.values():
            row.set_selectable(False)

    def _send_files(self, files: list, targets: list):
        provider = self._provider()
        if provider is None or not provider.port:
            self._toast(I18n.tr('chat_provider_na'))
            return
        mesh = self._mesh()
        if mesh is None:
            self._toast(I18n.tr('chat_send_fail'))
            return
        # 安全网：目录 getsize 不报错，会生成无效 offer（接收端打开目录才失败）
        files = _regular_files(files)
        if not files:
            self._toast(I18n.tr('chat_folder_unsupported'))
            return
        meta = []
        for path in files:
            name = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            meta.append({'name': name, 'size': size, 'path': path})
        for end_id in targets:
            # 每目标独立 session：FileProvider 同 (session_id,name) 会互相接管，
            # 群发复用同一 session 会让第二端拉取杀掉第一端连接。
            session_id = f"chat_{uuid.uuid4().hex}"
            token = uuid.uuid4().hex
            files_map = {m['name']: m['path'] for m in meta}
            provider.register_chat_session(session_id, token, files_map)
            self._out_sessions[session_id] = end_id
            data = Protocol.create_chat_file_offer(
                self._local_end_id(), uuid.uuid4().hex, session_id, token,
                [{'name': m['name'], 'size': m['size']} for m in meta],
                host=provider.host, port=provider.port, ttl=CHAT_TTL)
            if not mesh.send_to_peer(end_id, data):
                self._toast(I18n.tr('chat_send_fail'))
            # 本端回显：只表示"已发出"，对端是否接收不在本端可知范围（无回执协议）；
            # 记录源文件路径，双击打开 / 拖出另存都直接映射到该文件
            for m in meta:
                self._append_item(end_id, {
                    'kind': 'file', 'mine': True, 'name': m['name'],
                    'size': m['size'], 'state': 'sent', 'recv': 0, 'total': 0,
                    'end_id': end_id, 'path': m['path']})

    # ---- 文件接收 ----

    def _start_pull(self, item: dict):
        if item.get('state') not in ('pending', 'failed'):
            return
        if not item.get('host') or not item.get('port'):
            item['state'] = 'failed'
            self._update_file_item(item)
            return
        item['state'] = 'downloading'
        item['recv'] = 0
        item['total'] = int(item.get('size', 0) or 0)
        item['recv_log_key'] = f"chat_recv:{item['session_id']}:{item['name']}"
        self._update_file_item(item)
        t = threading.Thread(target=self._pull_worker, args=(item,), daemon=True)
        item['thread'] = t
        t.start()

    def _pull_worker(self, item: dict):
        key = item['key']
        dest = os.path.join(self._chat_dir(item['end_id']), item['name'])
        stop_ev = item['stop_event']
        log_key = item.get('recv_log_key') or ''

        def prog(recv, total):
            self._pull_progress.emit(key, int(recv), int(total))

        try:
            ok, total, err = pull_file(
                item['host'], int(item['port']), item['session_id'], item['token'],
                item['name'], dest, progress_cb=prog, stop_event=stop_ev,
                msg_type=MessageType.SYNC_PULL_REQ, max_resume=CHAT_PULL_MAX)
        except Exception as e:
            ok, total, err = False, 0, str(e)
        if stop_ev.is_set():
            # 取消：卡片状态由调用方处理，这里只收掉日志区进度行，避免残留
            self._recv_log_end.emit(log_key, item['name'], False)
            return
        self._pull_done.emit(key, bool(ok), err or '', dest if ok else '')
        self._recv_log_end.emit(log_key, item['name'], bool(ok))

    def _on_pull_progress(self, key: str, recv: int, total: int):
        item = self._items.get(key)
        if item is None:
            return
        item['recv'] = recv
        if total:
            item['total'] = total
        self._update_file_item(item)
        self._log_recv_progress(key, item, recv, total)

    def _log_recv_progress(self, key: str, item: dict, recv: int, total: int):
        """同步日志区进度条：接收端的私信文件进度也要像投递一样在日志可见。"""
        log_key = item.get('recv_log_key')
        log_fn = getattr(self._owner, 'chat_recv_progress', None)
        if log_key and callable(log_fn):
            log_fn(log_key, item.get('name', ''), int(recv), int(total))

    def _on_recv_log_end(self, log_key: str, name: str, ok: bool):
        """接收结束：日志区进度行转入历史记录（完成/失败）。"""
        log_fn = getattr(self._owner, 'chat_recv_finished', None)
        if callable(log_fn):
            log_fn(log_key, name, bool(ok))

    def _on_pull_done(self, key: str, ok: bool, err: str, path: str):
        item = self._items.get(key)
        if item is None:
            return
        if item.get('state') == 'expired':
            return
        item['state'] = 'done' if ok else 'failed'
        if ok:
            item['path'] = path
        self._update_file_item(item)

    # ---- 本地文件：打开 / 拖出另存 ----

    def _local_path(self, item: dict) -> str:
        """文件消息映射到的本地真实路径：接收端=落盘路径；发送端=发送时的源文件路径。

        发送端不回传副本，直接记录发送时选中的源文件位置，打开/拖出即映射到该文件。
        """
        if item.get('state') not in ('sent', 'done'):
            return ''
        return item.get('path') or ''

    def _require_local(self, item: dict) -> str:
        path = self._local_path(item)
        if not path or not os.path.exists(path):
            self._toast(I18n.tr('chat_file_no_local'))
            return ''
        return path

    def open_file(self, item: dict):
        """双击文件消息：用系统默认应用打开本地文件。"""
        path = self._require_local(item)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def start_drag_file(self, item: dict):
        """从文件消息拖出：以复制方式拖到目标位置另存（同同步列表，不移动源文件）。"""
        path = self._require_local(item)
        if not path:
            return
        attrs = self._clear_readonly(path)   # 另存副本不应把只读属性一并带过去
        drag = QDrag(self)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path)])
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        if attrs is not None:
            self._restore_attrs(path, attrs)

    @staticmethod
    def _clear_readonly(path: str):
        """Windows：临时摘掉只读属性，使拖出另存的副本可写；返回原属性值（未改动则 None）。"""
        if not Config.IS_WINDOWS:
            return None
        try:
            import ctypes
            readonly = 0x1   # FILE_ATTRIBUTE_READONLY
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
            if attrs in (None, -1) or not (attrs & readonly):
                return None
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs & ~readonly)
            return attrs
        except Exception:
            return None

    @staticmethod
    def _restore_attrs(path: str, attrs):
        try:
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(str(path), attrs)
        except Exception:
            pass

    # ---- 过期 ----

    def _expire_peer_items(self, end_id: str):
        """对端断线：其未接收/接收中的条目变灰"已过期"（已完成/失败保留）。"""
        for item in self._items.values():
            if item.get('end_id') != end_id or item.get('mine'):
                continue
            if item.get('state') in ('pending', 'downloading'):
                item['stop_event'].set()
                item['state'] = 'expired'
                self._update_file_item(item)

    def _expire_session(self, end_id: str, session_id: str):
        for item in self._items.values():
            if item.get('end_id') != end_id:
                continue
            if session_id and item.get('session_id') != session_id:
                continue
            if item.get('state') in ('pending', 'downloading'):
                item['stop_event'].set()
                item['state'] = 'expired'
                self._update_file_item(item)

    def _sweep_expired(self):
        now = time.time()
        for item in self._items.values():
            exp = item.get('expire_at') or 0
            if exp and now > exp and item.get('state') in ('pending', 'downloading'):
                item['stop_event'].set()
                item['state'] = 'expired'
                self._update_file_item(item)

    # ---- 收尾 ----

    def _toast(self, msg: str):
        add_log = getattr(self._owner, 'add_log', None)
        if callable(add_log):
            add_log("私信", msg)

    def shutdown(self):
        """退出房间：通知对端会话失效、清理本端私信会话与内容（不持久化）。"""
        self._ttl_timer.stop()
        self._remove_outside_filter()
        mesh = self._mesh()
        if mesh is not None:
            for session_id, end_id in self._out_sessions.items():
                try:
                    mesh.send_to_peer(
                        end_id, Protocol.create_chat_session_close(
                            self._local_end_id(), session_id, 'closed'))
                except Exception:
                    pass
        provider = self._provider()
        if provider is not None:
            try:
                provider.clear_chat_sessions()
            except Exception:
                pass
        for item in self._items.values():
            ev = item.get('stop_event')
            if ev is not None:
                ev.set()
            # 退房时在途接收不会再走到收尾信号，这里直接收掉日志区进度行
            log_key = item.get('recv_log_key')
            if log_key:
                self._on_recv_log_end(log_key, item.get('name', ''), False)
        self._out_sessions.clear()
        self._items.clear()
        self._history.clear()
        self._peers.clear()
        if self.handle is not None:
            self.handle.close()