"""
顶部快捷放置区（快捷添加文件） - 屏顶常驻 OLE 放置条（方案B，无全局钩子）

同步房间运行期间，在屏幕顶部常驻一条**跨屏宽、基本透明**的置顶窗口，它本身是
一个真实、非穿透的 OLE drop-target：

1. 用户把文件/文件夹从桌面（任何位置）往上拖到屏幕顶部，光标进入这条顶边带，系统
   直接派发 dragEnterEvent —— 不依赖任何全局鼠标钩子、不受会话隔离/DPI/UAC 影响。
2. 收到文件拖入 → 放置条马上从顶部**滑出可见胶囊**（"松开以添加文件到同步列表"），
   并显示真实文件数量。
3. 在胶囊上松手 → 文件加入当前同步列表（根目录即房间目录），随后收起；拖出顶带 →
   胶囊收回。

为什么不用"全屏 + WS_EX_TRANSPARENT 穿透覆盖层"或"全局低级鼠标钩子"：
- 点击穿透(WS_EX_TRANSPARENT)的窗口无法被系统 OLE 文件拖放命中，放置条根本不出现；
- 全局钩子(WH_MOUSE_LL)在本环境/真机上能装上却不派发回调，拖拽感知不可靠（两次实测无事件）。
- 本方案把判定交给系统 OLE 拖拽路由本身，天然可靠、且跨平台地由 Qt 兜底。

代价：房间运行时顶部会常驻一条(默认 56px)透明置顶带，拦截该区域的文件拖动/点击。
胶囊共存：若关联胶囊正悬浮在顶部，放置胶囊定位在其下方，避免两条浮条重叠。
"""
from PySide6.QtCore import Qt, QRectF, QByteArray, QVariantAnimation, QEasingCurve, Signal, Property, QPropertyAnimation, QPointF, QTimer
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QFontMetrics, QLinearGradient, QPalette, QFont
from PySide6.QtWidgets import QApplication, QWidget

from i18n import I18n

_STRIP_H = 56        # 激活状态下的全高；包含胶囊垂直空间
_THIN_H = 6          # 闲置状态下的顶条高度（极薄，减少点击遮挡）
_IDLE_W = 480        # 闲置时的中心段宽度（左右留白，不遮挡全屏软件边角快捷区）
_ANIM_MS = 260       # 展开/收起时长（OutCubic），匹配项目偏好
_WIDTH_MS = 110      # 内容切换时胶囊宽度伸缩时长（内容即时呈现，宽度平滑到位）
_TOP_GAP = 0         # 距屏幕可用区顶部的静止间距 → 0 即顶在屏幕最顶部
_HINT_TEXT = "松开以添加文件到同步列表"
_PLUS_W = 20         # idle 加号图标宽度（作为布局子件占位，文字永远在其右侧）
_TITLE_MAX_W = 160   # 进度态文件名最大宽度（过长时省略，防止进度条被挤出胶囊）
_PROGRESS_W = 420    # 进度态胶囊舒适宽度：给文件名与进度条充足呼吸空间


def _is_dark() -> bool:
    """当前系统主题是否为深色（用于文字/描边对比度）。"""
    c = QApplication.palette().color(QPalette.Window)
    return (c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114) < 128


def _local_file_paths(mime) -> list:
    """从拖拽 mime 提取存在的本地文件路径（去重）。"""
    seen, paths = set(), []
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        p = url.toLocalFile()
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)
    return paths


from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QLabel, QPushButton
from ui.widgets import BUTTON_STYLES

class _Pill(QWidget):
    """放置胶囊本体：一块胶囊，容纳不同状态的内容：
    - 默认：加号图标 + 提示文字（放置区展开时）
    - 确认替换：提示文字 + "确定"/"取消"两个按钮
    - 复制中：文件名 + 进度条
    - 完成：对勾勾选动画，完成后收起
    """

    _H = _STRIP_H
    # 状态枚举
    STATE_IDLE = 0       # 放置提示
    STATE_CONFIRM = 1    # 替换确认
    STATE_PROGRESS = 2   # 复制中
    STATE_DONE = 3       # 完成（对勾）

    confirm_clicked = Signal(bool)  # True=确定, False=取消
    done_finished = Signal()        # 完成对勾动画结束后发出（供收起）
    size_changed = Signal()         # 内容就位后发出，供 DropZone 平滑伸缩胶囊宽度

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(self._H)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._state = self.STATE_IDLE
        self._anim = None
        self._hovered = False
        self._init_ui()
        self.set_idle(0)

    def _init_ui(self):
        """单一横向内容行，各状态显隐不同子件，高度稳定在 _STRIP_H。

        过渡方式同局域网剪切板胶囊通告：内容即时应用（不做透明度交叉淡化），
        宽度交由 DropZone 平滑伸缩到位。idle 加号作为布局内的真实子件占位，
        文字始终位于其右侧，在胶囊由窄到宽的任意过渡阶段都不会与图标重叠。
        """
        self._content = QWidget(self)
        self._content.setAttribute(Qt.WA_TranslucentBackground, True)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._content)

        self._row = QHBoxLayout(self._content)
        self._row.setContentsMargins(20, 0, 20, 0)
        self._row.setSpacing(14)
        self._row.addStretch(1)

        # idle 加号图标：布局内子件，随内容流占位，杜绝与文字重叠
        self._plus = _PlusIcon(self._content)
        self._row.addWidget(self._plus, 0, Qt.AlignVCenter)

        # 完成态对勾（并入布局，与完成提示文字并排居中）
        self._check = _CheckMark(28, self._content)
        self._check.hide()
        self._row.addWidget(self._check, 0, Qt.AlignVCenter)

        # 标题：QLabel 不支持原生省略，超长内容由各状态 setter 用 QFontMetrics.elidedText 收缩为 "…"
        self._title = QLabel()
        self._title.setAttribute(Qt.WA_TranslucentBackground, True)
        self._row.addWidget(self._title, 0, Qt.AlignVCenter)

        # 进度条（复制中）：自绘，填充严格 0%→100% 铺满左右，无前后空档，文字居中自绘
        self._bar = _ProgressBar(self)
        self._bar.setMinimumWidth(220)
        self._bar.hide()
        self._row.addWidget(self._bar, 100, Qt.AlignVCenter)

        # 替换确认按钮
        self._ok_btn = QPushButton(I18n.tr('ok'))
        self._ok_btn.setStyleSheet(BUTTON_STYLES['primary'])
        self._ok_btn.setFixedSize(72, 28)
        self._ok_btn.clicked.connect(lambda: self.confirm_clicked.emit(True))
        self._row.addWidget(self._ok_btn, 0, Qt.AlignVCenter)
        self._cancel_btn = QPushButton(I18n.tr('cancel'))
        self._cancel_btn.setStyleSheet(BUTTON_STYLES['danger'])
        self._cancel_btn.setFixedSize(72, 28)
        self._cancel_btn.clicked.connect(lambda: self.confirm_clicked.emit(False))
        self._row.addWidget(self._cancel_btn, 0, Qt.AlignVCenter)
        self._ok_btn.hide()
        self._cancel_btn.hide()

        self._row.addStretch(1)

    def _is_dark(self) -> bool:
        c = QApplication.palette().color(QPalette.Window)
        return (c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114) < 128

    def _refresh_title_color(self):
        dark = self._is_dark()
        title = "#ffffff" if dark else "#1c2733"
        self._title.setStyleSheet(f"color: {title}; font-size: 13px; font-weight: 600;")

    def set_hovered(self, on: bool):
        """悬停态：仅影响描边强调，内容布局不变。"""
        self._hovered = on
        self.update()

    def is_busy(self) -> bool:
        """非 idle（确认/进度/完成）即处于执行中，此期间不接收新拖放。"""
        return self._state != self.STATE_IDLE

    def content_width(self) -> int:
        """当前内容的真实宽度：失效并激活布局后取 sizeHint（同胶囊 _content_size），
        得到稳定、确定的目标宽度，避免切换瞬间手算字体宽度随布局波动导致的抖动。"""
        c = self._content
        c.updateGeometry()
        lay = c.layout()
        lay.invalidate()
        lay.activate()
        return max(self._H, c.sizeHint().width())

    def _elide_text(self, text: str, max_w: int, mode=Qt.ElideRight) -> str:
        """把标题文本按像素宽度收缩为省略形式（默认右省略，可指定模式）。"""
        fm = QFontMetrics(self._title.font())
        if fm.horizontalAdvance(text) <= max_w:
            return text
        return fm.elidedText(text, mode, max_w)

    def _commit(self):
        """内容已即时应用：触发重绘并通知 DropZone 平滑伸缩胶囊宽度。"""
        self.update()
        self.size_changed.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)

    # --------------------------------------------------- 状态切换
    def _set_vis(self, show_title, show_bar, show_buttons, show_check):
        self._title.setVisible(show_title)
        self._title.setContentsMargins(0, 0, 0, 0)
        self._bar.setVisible(show_bar)
        self._ok_btn.setVisible(show_buttons)
        self._cancel_btn.setVisible(show_buttons)
        self._check.setVisible(show_check)
        self._plus.setVisible(self._state == self.STATE_IDLE)
        self._refresh_title_color()

    def set_idle(self, count: int):
        """回到初始：加号 + 松开添加提示"""
        self._state = self.STATE_IDLE
        text = _HINT_TEXT if count <= 1 else f"松开以添加 {count} 个文件到同步列表"
        el = self._elide_text(text, 460)

        self._title.setText(el)
        self._title.setMaximumWidth(460)
        self._row.setContentsMargins(20, 0, 20, 0)
        self._set_vis(True, False, False, False)
        self._commit()

    def set_confirm_replace(self, existing_names: list):
        """切换到替换确认：标题 + 确定/取消按钮。

        单个文件名过长时用 middle 省略保住句尾「是否替换?」，多文件只显示数量，
        避免长列表/长句在胶囊内被右侧裁剪、出现"是否…"被截断的观感。
        """
        self._state = self.STATE_CONFIRM
        n = len(existing_names)
        if n == 1:
            title = self._elide_text(
                I18n.tr('file_exists_replace') % existing_names[0], 340, Qt.ElideMiddle)
        else:
            title = I18n.tr('files_exist_replace') % n

        self._title.setText(title)
        self._title.setMaximumWidth(420)
        self._row.setContentsMargins(20, 0, 20, 0)
        self._set_vis(True, False, True, False)
        self._commit()

    def set_progress(self, filename: str, progress: int):
        """进入复制进度：文件名(左, 省略) + 自绘进度条。

        进度条填充严格从自身左缘(0%)延展到右缘(100%)，无前后空档。
        """
        self._state = self.STATE_PROGRESS
        prog = max(0, min(100, progress))
        name = self._elide_text(filename, _TITLE_MAX_W)

        self._title.setText(name)
        self._title.setMaximumWidth(_TITLE_MAX_W)
        self._row.setContentsMargins(14, 0, 14, 0)  # 进度态收紧左右，让进度条更贴边
        self._bar.set_frac(prog / 100.0)
        self._bar.set_text(f"{prog}%")
        self._set_vis(True, True, False, False)
        self._commit()

    def set_progress_value(self, progress: int):
        """复制过程中的增量进度刷新：仅改填充比例与文字，不重置状态/不触发宽度动画，避免频闪。"""
        prog = max(0, min(100, progress))
        self._bar.set_frac(prog / 100.0)
        self._bar.set_text(f"{prog}%")
        # QProgressBar 自动重绘增量；无需触碰标题与可见性

    def set_done(self):
        """切换到完成：对勾 + "完成"提示文字，随后播放勾选动画。"""
        self._state = self.STATE_DONE
        done_text = I18n.tr('add_done')

        self._title.setText(done_text)
        self._title.setMaximumWidth(360)
        self._row.setContentsMargins(20, 0, 20, 0)
        self._set_vis(True, False, False, True)
        self._commit()

        # 对勾生长动画（内容即位后播放，完成后发 done_finished 供收起）
        anim = QPropertyAnimation(self._check, QByteArray(b"progress"), self)
        anim.setDuration(260)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.finished.connect(self.done_finished.emit)
        anim.start()
        self._anim = anim

    # --------------------------------------------------- 绘制（胶囊背景、边框）
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        bg = QApplication.palette().color(QPalette.Window)
        dark = bg.lightness() < 128
        top = bg.lighter(150) if dark else bg.lighter(105)
        grad = QLinearGradient(0, 0, 0, rect.height())
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bg)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(rect, radius, radius)

        # 描边：淡灰色（随主题微调对比度），避免醒目蓝边
        border = QColor(200, 200, 200) if not dark else QColor(122, 122, 122)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(
            QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
            radius - 0.5, radius - 0.5)
        p.end()


# --------------------------------------------------------- idle 加号图标（布局子件）
class _PlusIcon(QWidget):
    """idle 状态左侧的加号图标：作为布局内真实子件占位，
    保证任何胶囊宽度下文字都排在其右侧、不发生重叠。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(_PLUS_W, _PLUS_W)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        cx, cy = self.width() / 2.0, self.height() / 2.0
        pen = QPen(QColor("#74c0fc"), 2)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        h = 14
        p.drawLine(QPointF(cx, cy - h / 2), QPointF(cx, cy + h / 2))
        p.drawLine(QPointF(cx - h / 2, cy), QPointF(cx + h / 2, cy))
        p.end()


# --------------------------------------------------------- 对勾绘制动画（同 capsule_notification）
class _CheckMark(QWidget):
    """自绘对钩动画（从左上到右下线性绘制，描边随进度生长）。"""

    def __init__(self, size=28, parent=None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._progress = 0.0

    def get_progress(self) -> float:
        return self._progress

    def set_progress(self, value: float):
        self._progress = max(0.0, min(1.0, value))
        self.update()

    progress = Property(float, get_progress, set_progress)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        # 圆底
        p.setBrush(QColor("#51cf66"))
        p.drawEllipse(QRectF(0, 0, self.width(), self.height()))

        # 对钩折线路径
        path = QPainterPath()
        path.moveTo(self.width() * 0.28, self.height() * 0.52)
        path.lineTo(self.width() * 0.44, self.height() * 0.68)
        path.lineTo(self.width() * 0.73, self.height() * 0.36)

        pen = QPen(QColor("#ffffff"), 3)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        # 用裁剪来限制已绘制部分：仅绘制矩形 (0,0)->(宽, 高*进度) 内的对钩
        p.setPen(pen)
        p.save()
        clip = QRectF(0, 0, self.width(), self.height() * self._progress)
        p.setClipRect(clip)
        p.drawPath(path)
        p.restore()
        p.end()


class _ProgressBar(QWidget):
    """自绘进度条：填充严格从自身左缘(0%)延展到右缘(100%)，无前后空档；
    百分比文字居中自绘，避免 QProgressBar 轨道内缩及文字丢失问题。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frac = 0.0
        self._text = "0%"
        self.setMinimumHeight(24)
        self._is_dark_flag = None  # 惰性读取主题

    @staticmethod
    def _is_dark() -> bool:
        c = QApplication.palette().color(QPalette.Window)
        return (c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114) < 128

    def set_frac(self, frac: float):
        self._frac = max(0.0, min(1.0, frac))
        self.update()

    def set_text(self, text: str):
        self._text = text
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        r = h / 2.0
        track = QRectF(0.5, 0.5, w - 1.0, h - 1.0)
        dark = self._is_dark()

        # 轨道（淡灰，随主题微调）
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(150, 160, 175, 60) if dark else QColor(150, 160, 175, 40))
        p.drawRoundedRect(track, r, r)

        # 填充：从左缘起 0 -> w*frac，用轨道圆角裁剪，前缘/后缘满铺
        fw = w * self._frac
        if fw > 0.0:
            p.save()
            path = QPainterPath()
            path.addRoundedRect(track, r, r)
            p.setClipPath(path)
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, QColor("#a9d9ff" if not dark else "#7cc4fc"))
            grad.setColorAt(1.0, QColor("#4aa4e8"))
            p.setBrush(grad)
            p.drawRect(QRectF(0.0, 0.0, fw, h))
            p.restore()

        # 居中百分比文字：用每主题单一颜色，保证在轨道与填充上都有足够对比，
        # 不在 50% 文字交界处随填充前沿做颜色硬切（那会在同一像素产生一次观感卡顿）。
        # 深色主题数字用白、浅色主题用深海军蓝，均可在灰轨道与蓝填充上清晰识读。
        font = QFont(self.font())
        font.setPointSizeF(9.0)
        font.setWeight(QFont.DemiBold)
        p.setFont(font)
        fm = QFontMetrics(font)
        text = fm.elidedText(self._text, Qt.ElideRight, max(10, w - 12))
        pen = QColor("#ffffff") if dark else QColor("#0b2a47")
        p.setPen(pen)
        p.drawText(track, Qt.AlignCenter, text)
        p.end()


class DropZone(QWidget):
    """屏顶常驻 OLE 放置条：跨屏宽透明置顶窗，收到文件拖入即滑出胶囊，松手添加。

    房间就绪后构造并 show()，close 事件里 close()。无需轮询/钩子。
    """

    files_added = Signal(list)
    confirm_clicked = Signal(bool)  # 由胶囊按钮转发：True=确定, False=取消

    def __init__(self, add_callback=None, capsule=None):
        super().__init__()
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAcceptDrops(True)   # 非穿透，OLE 拖放可命中

        self._add_callback = add_callback or (lambda paths: False)
        self._capsule = capsule
        self._copy_worker = None

        # 放置胶囊（可见那块），始终是子项，由 _reveal 控制位置/显隐
        self._pill = _Pill(self)
        self._pill.hide()
        self._pill.confirm_clicked.connect(self.confirm_clicked)
        self._pill.done_finished.connect(self._on_done_finished)
        self._pill.size_changed.connect(self._sync_full_w)
        self._done_timer = None

        # 胶囊滑入(0→1)/滑出(1→0)，同时驱动宽度从小到满
        self._reveal = QVariantAnimation(self)
        self._reveal.setDuration(_ANIM_MS)
        self._reveal.setEasingCurve(QEasingCurve.OutCubic)
        self._reveal.setStartValue(0.0)
        self._reveal.setEndValue(1.0)
        self._reveal.valueChanged.connect(self._apply_reveal)

        self._active = False
        self._full_w = _STRIP_H
        self._left = _STRIP_H
        self._width_anim = None
        self._set_geometry_fullwidth()
        self._apply_reveal(0.0)
        self._pill.hide()

    # ------------------------------------------------------------- 对外
    def set_add_callback(self, fn):
        self._add_callback = fn or (lambda paths: False)

    def set_capsule(self, capsule):
        self._capsule = capsule

    def is_active(self) -> bool:
        return self._active

    # ---- 胶囊状态机（同步窗口最小化时由外部驱动：确认替换→进度→完成收起）
    def _on_done_finished(self):
        """完成对勾动画结束后，停留 1s 再收起，便于用户看清「添加完成」提示。"""
        self._clear_done_hang()
        self._done_timer = QTimer(self)
        self._done_timer.setSingleShot(True)
        self._done_timer.timeout.connect(self._collapse)
        self._done_timer.start(1000)

    def _clear_done_hang(self):
        """清除待执行的完成收起计时（重新进入任一状态或新拖拽时调用）。"""
        if self._done_timer is not None:
            self._done_timer.stop()
            self._done_timer = None

    def _sync_full_w(self):
        """内容切换应用后：把胶囊宽度平滑伸缩到目标。

        由 _Pill.size_changed 触发。仅在**完全展开后**才动画伸缩宽度（同胶囊
        _present 只在 _expand>=0.99 时 _resize_to），否则两个动画抢写宽度会抖动；
        未完全展开时只更新目标宽度，交给 reveal 一次性长到位。
        """
        new_w = self._pill.content_width()
        revealed = (self._reveal.currentValue() or 0.0) >= 0.98
        if self._active and revealed:
            self._animate_to_width(new_w)
        else:
            self._full_w = new_w

    def _animate_to_width(self, new_w: int):
        """已展开时，把胶囊宽度从当前值平滑过渡到 new_w。

        不要先抢写 _full_w 为终点值——否则在动画首帧插值前，任何读取 _full_w 的
        路径（_apply_width→_apply_reveal）会瞬间跳到终点宽度再回摆，造成闪烁/抖动。
        改为让动画逐帧插值驱动 _full_w，结束时精确兜底为 new_w。
        """
        if self._width_anim is not None:
            self._width_anim.stop()
            self._width_anim = None
        old = self._full_w
        if old == new_w:
            self._full_w = new_w
            self._apply_reveal(self._reveal.currentValue() or 0.0)
            return
        anim = QVariantAnimation(self)
        anim.setDuration(_WIDTH_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(float(old))
        anim.setEndValue(float(new_w))
        anim.valueChanged.connect(self._apply_width)
        anim.finished.connect(lambda: setattr(self, '_full_w', float(new_w)))
        anim.start()
        self._width_anim = anim

    def _apply_width(self, v: float):
        """宽度插值回调：以当前展开度刷新几何。"""
        self._full_w = v
        r = self._reveal.currentValue()
        self._apply_reveal(1.0 if (r is None or r < 0.01) else r)

    def _ensure_expanded(self):
        """确保胶囊处于完全展开姿态。"""
        self._set_active(True)

    def _collapse(self):
        """收起胶囊（取消/完成动画结束后调用）。"""
        self._set_active(False)
        self._pill.set_hovered(False)

    def dismiss(self):
        """取消替换等场景：收起胶囊，不执行复制。"""
        self._collapse()

    def begin_confirm_replace(self, existing_names: list):
        """进入替换确认：胶囊显示标题 + 确定/取消，保持展开待用户抉择。"""
        self._clear_done_hang()
        self._pill.set_confirm_replace(existing_names)
        self._ensure_expanded()
        self.show()  # 确保窗口可见（即使在最小化等场景下被系统时序掩盖）

    def begin_progress(self, filename: str, progress: int = 0):
        """进入复制进度：胶囊显示文件名 + 进度条。"""
        self._clear_done_hang()
        self._pill.set_progress(filename, progress)
        self._ensure_expanded()
        self.show()

    def update_progress(self, filename: str, progress: int):
        """复制进度更新：仅刷新进度条数值，不重置状态/不触发淡入（避免频闪）。"""
        self._pill.set_progress_value(progress)

    def finish_done(self):
        """完成：播放对勾动画，结束后自动收起（done_finished→_collapse）。"""
        self._pill.set_done()
        self._ensure_expanded()

    # ------------------------------------------------------------- 定位
    def _screen(self):
        return QApplication.primaryScreen().availableGeometry()

    def _obstacle_y(self) -> int:
        S = self._screen()
        cap = self._capsule
        if cap is not None and cap.isVisible():
            return cap.frameGeometry().bottom() + 14
        return S.y() + _TOP_GAP

    def _set_geometry_fullwidth(self):
        """闲置态：顶条收回屏幕中心的一段(_IDLE_W 宽、_THIN_H 高)，左右留白，
        不遮挡全屏软件边角快捷区；激活时向左右延伸亦以中心为锚。"""
        S = self._screen()
        w = min(_IDLE_W, S.width())
        self.setFixedSize(w, _THIN_H)
        self.move(S.x() + (S.width() - w) // 2, S.y())

    def _pill_bottom(self) -> int:
        """胶囊静止时底缘的屏幕 y（本地 y + 顶边带 y）。"""
        return self.y() + _STRIP_H

    def _apply_reveal(self, v: float):
        """按 v(0..1) 展开/收起：窗口宽度由中心段滑动延伸，高度在
        _THIN_H↔_STRIP_H，始终以屏幕中心为锚（左右对称）；胶囊同步滑入/滑出。"""
        cn = max(0.0, min(1.0, v))
        S = self._screen()
        expanded_w = max(_IDLE_W, self._full_w, _STRIP_H)
        w_win = int(_IDLE_W + (expanded_w - _IDLE_W) * cn)
        h_win = int(_THIN_H + (_STRIP_H - _THIN_H) * cn)
        win_x = S.x() + (S.width() - w_win) // 2
        self.setFixedSize(w_win, h_win)
        self.move(win_x, S.y())

        h = _STRIP_H
        w = int(h + (self._full_w - h) * cn)
        # 顶边带本地坐 y：静止 0，滑入起点为 -(h+8)（父窗顶部边界，天然被裁剪成滑入效果）
        y = int(round(-(h + 8) * (1.0 - cn)))
        x = self.width() // 2 - w // 2   # 相对父窗中心
        self._pill.setGeometry(x, y, w, h)
        self._pill.setVisible(cn > 0.01)

    def _set_active(self, on: bool):
        """滑出胶囊(on)或收起到顶外(off)。"""
        if not on and self._width_anim is not None:
            self._width_anim.stop()
            self._width_anim = None
        if on == self._active:
            self._apply_reveal(self._reveal.currentValue() or 0.0)
            return
        self._active = on
        cur = self._reveal.currentValue()
        if cur is None:
            cur = 0.0
        self._reveal.stop()
        self._reveal.setStartValue(cur)
        self._reveal.setEndValue(1.0 if on else 0.0)
        self._reveal.start()

    def _refresh_text(self, count: int):
        self._pill.set_idle(count)
        return self._pill.content_width()

    # ------------------------------------------------------------- 拖拽
    def _on_pill(self, event) -> bool:
        return self._pill.isVisible() and \
            self._pill.geometry().contains(event.position().toPoint())

    def dragEnterEvent(self, event):
        # 胶囊正在执行内容（确认/复制/完成）时一律拒绝新拖放，等完成收起后再接
        if self._active and self._pill.is_busy():
            event.ignore()
            return
        paths = _local_file_paths(event.mimeData())
        if paths:
            self._clear_done_hang()
            self._full_w = self._refresh_text(len(paths))
            self._set_active(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        paths = _local_file_paths(event.mimeData())
        if paths:
            # 整个顶部放置带都接收鼠标移动悬停，确保不被拒绝
            event.acceptProposedAction()
            self._pill.set_hovered(self._on_pill(event))
        else:
            event.ignore()
            self._pill.set_hovered(False)

    def dragLeaveEvent(self, event):
        self._pill.set_hovered(False)
        self._set_active(False)

    def dropEvent(self, event):
        """只要松手落在顶部放置带内就触发添加回调；若回调返回 True（表示要走胶囊流程，
        如替换确认/复制进度），则保持胶囊展开继续承接，否则收回到顶外。"""
        # 执行中被拒绝的拖放在此兜底拦截，避免绕过 dragEnterEvent 的状态破坏
        if self._active and self._pill.is_busy():
            event.ignore()
            return
        paths = _local_file_paths(event.mimeData())
        keep_open = False
        if paths:
            try:
                keep_open = bool(self._add_callback(paths))
            except Exception:
                keep_open = False
            self.files_added.emit(list(paths))
        if not keep_open:
            self._pill.set_hovered(False)
            self._set_active(False)
        event.acceptProposedAction()

    # ------------------------------------------------------------- 原生
    def paintEvent(self, event):
        """检测区画一层 alpha≈1 的「隐形填充」。

        关键机制：`WA_TranslucentBackground` 使窗口成为 Windows 分层窗口；
        若 paintEvent 不画任何内容，窗口根本不会被系统落实，因而收不到 OLE 拖放
        （这就是此前"透明=不启用"的根因）。但只要画上哪怕极低的 alpha，
        窗口就会被 UpdateLayeredWindow 落实为可命中的拖放目标；而命中与 alpha
        大小无关，因此 alpha≈1 即可肉眼完全不可见，却可靠接收拖放。
        """
        # 单个全屏像素级填充，alpha=1：真正透明(视觉上零残留)，却让分层窗口有实体、可接收拖放
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 1))
        p.end()

    def showEvent(self, event):
        super().showEvent(event)
        self._set_geometry_fullwidth()