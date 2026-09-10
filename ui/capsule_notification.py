"""
胶囊通知栏 - 局域网剪切板文件投递的三段式状态展示

与 CapRise 倒计时结束通知 (TimerNoticeOverlay) 一致，是一条独立的系统级胶囊：
无边框 + 置顶 + Tool 的顶层窗口，透明背景，不抢焦点；显示在**屏幕顶部居中**，
完全脱离主窗口/同步界面，随屏幕可用区自动摆位（同 CapRise 倒计时结束通知）。

表现文件投递的三个阶段：
    1. 收到通知   : “有可用的远程文件 {名称} {大小}”，提示可用 Ctrl+V 粘贴
    2. 开始传输   : 收起提示，露出文件名 + 淡蓝进度条
    3. 完成       : 进度条填满后，以对钩动画淡出隐藏

风格与动画约定（本分支自行实现）：
- 本体样式沿用常用胶囊栏的胶囊观感：系统背景派生的纵向渐变 + 发丝描边
  （深色下 #505050），非固定蓝色，不做玻璃拟态。进度条仍为淡蓝色（#74c0fc）。
- 出现 / 收起：自屏幕上方滑入，宽度由小药丸线性伸展；收起时上滑 + 淡出。
  不使用常驻透明度效果——它缓存源图，重新展开时会产生文字纵向重叠的残留。
- 完成后：圆角胶囊内播放对钩绘制动画，随后整条收起淡出。
"""
from PySide6.QtCore import Qt, QRect, QRectF, QPoint, QParallelAnimationGroup, QPropertyAnimation, Property, QEasingCurve, Signal, QEvent, QByteArray
from PySide6.QtGui import QPainter, QColor, QPen, QPainterPath, QLinearGradient, QPalette
from PySide6.QtWidgets import (
    QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel,
    QProgressBar, QSizePolicy, QGraphicsOpacityEffect,
)


def format_bytes(num: int) -> str:
    """把字节数格式化为人类可读的尺寸字符串。"""
    size = float(num)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if size < 1024 or unit == 'TB':
            return f"{size:.0f} {unit}" if unit == 'B' else f"{size:.1f} {unit}"
        size /= 1024


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
        # 用 pathElementAt 按进度截取局部路径长度无法直接实现，改用蒙版实现逐段生长
        p.setPen(pen)
        # 用裁剪来限制已绘制部分：仅绘制矩形 (0,0)->(宽, 高*进度) 内的对钩
        p.save()
        clip = QRectF(0, 0, self.width(), self.height() * self._progress)
        p.setClipRect(clip)
        p.drawPath(path)
        p.restore()
        p.end()


class CapsuleNotification(QFrame):
    """屏幕顶部居中的独立顶层胶囊通知栏（类似 CapRise 倒计时结束通知）。"""

    # 点击胶囊请求粘贴远程文件（收到通知状态下点击触发热点）
    paste_requested = Signal()

    _TOP_GAP = 40         # 距屏幕可用区顶部的静止间距（同 CapRise 倒计时结束通知）
    _ANIM_MS = 300        # 展开/收起滑动时长
    _TRANSITION_MS = 260  # 内容长度伸缩过渡时长
    _EASE = QEasingCurve.OutCubic

    def __init__(self, parent=None):
        super().__init__(parent)
        # 独立系统级通知：无边框 + 置顶 + Tool 顶层窗口，透明背景，不抢焦点。
        # 脱离任何主窗口/同步界面，位置由屏幕可用区决定（见 _place）。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

        # 状态
        self._expand = 0.0   # 0 收起（小药丸，沉到屏幕下方） -> 1 完全展开
        self._full_w = 0     # 当前内容完整宽度（测量后使用）
        self._min_w = 44     # 收起态最小胶囊宽度（中心锚定药丸）
        # 淡出动画：仅收起时临时挂载透明度效果，隐藏落点立即移除，
        # 避免效果缓存源图导致重新展开时文字重叠的老问题。
        self._fade_effect = None
        self._fade_anim = None

        self._init_ui()

        # 展开/收起动画：展开驱动位移、宽度揭示（mask）与透明度
        self._anim = QPropertyAnimation(self, QByteArray(b"expand"), self)
        self._anim.setDuration(self._ANIM_MS)
        self._anim.setEasingCurve(self._EASE)

        # 对钩绘制动画（作用于布局内的状态图标）
        self._check_anim = QPropertyAnimation(self._icon, QByteArray(b"progress"), self)
        self._check_anim.setDuration(260)
        self._check_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._check_anim.finished.connect(self._on_check_done)

        # 内容长度（宽/高）动态伸缩动画：随内容变化平滑适配
        self._resize_group = QParallelAnimationGroup(self)
        self._w_anim = QPropertyAnimation(self, QByteArray(b"contentWidth"), self)
        self._w_anim.setDuration(self._TRANSITION_MS)
        self._w_anim.setEasingCurve(self._EASE)
        self._h_anim = QPropertyAnimation(self, QByteArray(b"contentHeight"), self)
        self._h_anim.setDuration(self._TRANSITION_MS)
        self._h_anim.setEasingCurve(self._EASE)
        self._resize_group.addAnimation(self._w_anim)
        self._resize_group.addAnimation(self._h_anim)

        self._wait_timer = None      # 完成态停留计时
        self._initially_hidden = True
        self.setVisible(False)
        self.raise_()

        # 收起动画到底后真正隐藏（只认动画目标方向）
        self._anim_target = 1.0
        self._anim.finished.connect(self._on_anim_finished)

    # ------------------------------------------------------------------ UI

    def _init_ui(self):
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 内容容器：把图标/文本/进度条包在一起（不套透明度效果，避免嵌套冲突）
        self._content = QWidget(self)
        inner = QHBoxLayout(self._content)
        inner.setContentsMargins(20, 10, 20, 10)
        inner.setSpacing(14)

        # 左侧状态图标区（对钩仅在完成态显示）
        self._icon = _CheckMark(28)
        self._icon.setVisible(False)
        inner.addWidget(self._icon, 0, Qt.AlignVCenter)

        # 文本内容区：整体作为一个可垂直居中的子控件
        self._text_box = QWidget()
        text_col = QVBoxLayout(self._text_box)
        text_col.setSpacing(3)
        text_col.setContentsMargins(0, 0, 0, 0)

        self._title = QLabel()
        self._title.setTextInteractionFlags(Qt.NoTextInteraction)
        text_col.addWidget(self._title)

        self._hint = QLabel()
        self._hint.setVisible(False)
        text_col.addWidget(self._hint)

        inner.addWidget(self._text_box, 0, Qt.AlignVCenter)

        # 传输进度条：显示「百分比 + 已接收/总量」（传输态出现）
        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFixedWidth(280)
        self._bar.setFixedHeight(20)
        self._bar_style("#94a3b8")
        self._bar.setVisible(False)
        inner.addWidget(self._bar, 0, Qt.AlignVCenter)

        outer.addWidget(self._content)

    def show_available(self, file_name: str, total_bytes: int):
        """状态1 —— 收到通知：提示“有可用的远程文件 {名称} {大小}”。"""
        self._stop_all()
        self._refresh_theme()
        self._title.setText(f"有可用的远程文件  {file_name}  {format_bytes(total_bytes)}")
        self._hint.setText("按 Ctrl+V 粘贴到此设备")
        self._hint.setVisible(True)
        self._bar.setVisible(False)
        self._bar.setValue(0)
        self._icon.setVisible(False)
        self._present()

    def begin_transfer(self, file_name: str, total_bytes: int):
        """状态2 —— 开始传输：露出文件名 + 淡蓝进度条（从 0 起步）。"""
        self._stop_all()
        self._refresh_theme()
        self._title.setText(file_name)
        self._hint.setText(f"正在投递  {format_bytes(total_bytes)}")
        self._hint.setVisible(True)
        self._bar.setVisible(True)
        self._bar.setEnabled(True)
        self._bar.setValue(0)
        self._bar.setFormat(f"0%  ·  0 B / {format_bytes(total_bytes)}")
        self._icon.setVisible(False)
        self._present()

    def set_progress(self, received: int, total: int):
        """更新传输进度（received / total），同步显示百分比与已接收/总量。"""
        if not self._bar.isVisible():
            self._bar.setVisible(True)
        self._bar.setEnabled(True)
        percent = int(received / total * 100) if total > 0 else 0
        self._bar.setValue(max(0, min(1000, int(received / total * 1000)
                                      if total > 0 else 0)))
        if total > 0:
            self._bar.setFormat(
                f"{percent}%  ·  {format_bytes(received)} / {format_bytes(total)}")

    def complete(self):
        """状态3 —— 完成：进度封顶，播放对钩动画后淡出隐藏。"""
        if not self.isVisible():
            return
        self._bar.setValue(1000)
        self._bar.setVisible(False)
        self._hint.setVisible(False)
        self._icon.setVisible(True)
        # 完成态收起提示与进度条，内容整体淡入 + 长度收缩，随后播放对钩
        self._present()
        self._check_anim.stop()
        self._check_anim.setStartValue(0.0)
        self._check_anim.setEndValue(1.0)
        self._check_anim.start()

    def dismiss(self):
        """手动收起（动画）"""
        self._hide_anim()

    def mouseReleaseEvent(self, event):
        """点击胶囊（收到通知态）请求粘贴远程文件。"""
        if event.button() == Qt.LeftButton and self._bar.isVisible() is False:
            self.paste_requested.emit()
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------------- 动画

    def _content_size(self):
        """当前内容的完整尺寸（宽/高），作为长度伸缩的目标。

        收起/hide 后子部件 sizeHint 会缓存为空内容，重新展开前必须先
        失效并激活布局，否则量出过小尺寸导致内容溢出/文字重叠。
        """
        c = self._content
        c.updateGeometry()          # 通知父级本控件几何需求变化
        lay = c.layout()
        lay.invalidate()            # 丢弃缓存的 sizeHint
        lay.activate()
        sh = c.sizeHint()
        return max(self._min_w, sh.width()), max(40, sh.height())

    def _present(self):
        """内容就位后统一呈现：已完全展开则长度伸缩，否则走展开动画。"""
        if self.isVisible() and self._expand >= 0.99:
            w, h = self._content_size()
            self._full_w = w
            self._resize_to(w, h)
        else:
            self._show_anim()

    def _resize_to(self, target_w: int, target_h: int):
        """内容长度动态伸缩：宽/高并行平滑动画适配到目标尺寸。"""
        target_w = max(self._min_w, int(target_w))
        target_h = max(40, int(target_h))
        if abs(target_w - self.width()) < 1 and abs(target_h - self.height()) < 1:
            return
        self._resize_group.stop()
        self._w_anim.setStartValue(self.width())
        self._w_anim.setEndValue(float(target_w))
        self._h_anim.setStartValue(self.height())
        self._h_anim.setEndValue(float(target_h))
        self._resize_group.start()

    def _show_anim(self):
        self._anim_target = 1.0
        self._clear_fade()           # 中断收起淡出时移除法，恢复干净展开
        self._resize_group.stop()
        # 顶层窗口须先显示再测量：隐藏状态下 isVisible() 为 False，布局会误把
        # 提示文本等子项排除，量出过小高度导致内容溢出/文字纵向重叠。
        self.setFixedWidth(self._min_w)      # 起手为小药丸，再由 expand 扩张到完整宽度
        self._place(0.0, self._min_w)        # 先摆到屏幕下方（收起位），避免首帧闪现
        self.setVisible(True)
        self.raise_()
        w, h = self._content_size()
        self._full_w = w
        self.setFixedHeight(h)
        self._anim.stop()
        self._anim.setStartValue(self._expand)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def _hide_anim(self):
        if self._expand <= 0.001 and not self.isVisible():
            self._anim_target = 0.0
            return
        self._anim_target = 0.0
        self._check_anim.stop()
        self._resize_group.stop()
        self._full_w = max(self._min_w, self.width())  # 从当前完整宽度收窄到小药丸
        self._anim.stop()
        self._anim.setStartValue(self._expand)
        self._anim.setEndValue(0.0)
        self._anim.start()
        # 淡出与本条几何收起并行；隐藏落点会移除效果，避免缓存残留
        self._start_fade_out()

    def _start_fade_out(self):
        """收起时挂载临时透明度效果并淡出（仅射线收起，非展开状态）。"""
        self._clear_fade()
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(1.0)
        self.setGraphicsEffect(effect)
        anim = QPropertyAnimation(effect, QByteArray(b"opacity"), self)
        anim.setDuration(int(self._ANIM_MS * 0.9))
        anim.setStartValue(1.0)
        anim.setEndValue(0.0)
        anim.setEasingCurve(self._EASE)
        anim.start()
        self._fade_effect = effect
        self._fade_anim = anim

    def _clear_fade(self):
        """移除透明度效果（收起落点/重新展开前调用），恢复直接绘制。"""
        if self._fade_anim is not None:
            self._fade_anim.stop()
            self._fade_anim = None
        if self._fade_effect is not None:
            self.setGraphicsEffect(None)
            self._fade_effect = None

    def _on_anim_finished(self):
        # 收起方向滑到底才真正隐藏，避免展开首帧误触发 setVisible(False)
        if self._anim_target <= 0.0:
            self._expand = 0.0
            self.hide()
            self._clear_fade()   # 隐藏后丢弃透明度效果，防止缓存源图残留
            # 收起后重置全部内容状态，下次单独拉出时从干净状态展开，
            # 避免旧文字残留导致纵向重叠等问题
            self._reset_content()

    def _reset_content(self):
        """清空提示/标题/进度条/对钩，回到初始空白状态。"""
        self._title.clear()
        self._hint.clear()
        self._hint.setVisible(False)
        self._bar.setVisible(False)
        self._bar.setValue(0)
        self._icon.setVisible(False)

    def _stop_all(self):
        self._anim.stop()
        self._check_anim.stop()
        self._resize_group.stop()

    def _on_check_done(self):
        # 对钩停留片刻后淡出
        from PySide6.QtCore import QTimer
        if self._wait_timer is not None:
            self._wait_timer.stop()
        self._wait_timer = QTimer(self)
        self._wait_timer.setSingleShot(True)
        self._wait_timer.timeout.connect(self._hide_anim)
        self._wait_timer.start(320)

    # ----------------------------------------------------------------- 主题

    def _is_dark(self) -> bool:
        from PySide6.QtGui import QPalette
        win = self.window()
        pal = win.palette() if (win is not None) else self.palette()
        c = pal.color(QPalette.Window)
        return (c.red() * 0.299 + c.green() * 0.587 + c.blue() * 0.114) < 128

    def _bar_style(self, text_color: str):
        """胶囊进度条样式：中性轨道 + 淡蓝块 + 主题化文字（百分比/已接收/总量）。"""
        self._bar.setStyleSheet(f"""
            QProgressBar {{
                background-color: rgba(120, 120, 120, 0.22);
                border: none;
                border-radius: 10px;
                text-align: center;
                color: {text_color};
                font-size: 11px;
                font-weight: 600;
                padding: 0px 6px;
            }}
            QProgressBar::chunk {{
                background-color: #74c0fc;
                border-radius: 9px;
                margin: 1px;
            }}
        """)

    def _refresh_theme(self):
        """按当前深浅主题刷新文字颜色（深色下数值用白、标签用灰）。"""
        dark = self._is_dark()
        if dark:
            title, hint, bar_text = "#ffffff", "#a0a0a0", "#e2e8f0"
        else:
            title, hint, bar_text = "#1c2733", "#74818f", "#475569"
        self._title.setStyleSheet(f"color: {title}; font-size: 13px; font-weight: 600;")
        self._hint.setStyleSheet(f"color: {hint}; font-size: 11px;")
        self._bar_style(bar_text)

    def paintEvent(self, event):
        # CapRise 主胶囊栏默认样式：系统背景派生的纵向渐变 + 深色下发丝描边，
        # 无固定蓝色、无玻璃拟态
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        radius = rect.height() / 2.0
        bg = QApplication.palette().color(QPalette.Window)
        is_dark = bg.lightness() < 128
        top = bg.lighter(150) if is_dark else bg.lighter(105)
        grad = QLinearGradient(0, 0, 0, rect.height())
        grad.setColorAt(0.0, top)
        grad.setColorAt(1.0, bg)
        p.setBrush(grad)
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(rect, radius, radius)
        p.setBrush(Qt.NoBrush)
        if is_dark:
            p.setPen(QPen(QColor(80, 80, 80, 255), 1))
            p.drawRoundedRect(QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
                              radius - 0.5, radius - 0.5)
        p.end()
        super().paintEvent(event)

    # ------------------------------------------------------------ expand

    def get_expand(self) -> float:
        return self._expand

    def set_expand(self, value: float):
        value = max(0.0, min(1.0, value))
        self._expand = value
        if not self.isVisible() and value > 0.0:
            self.setVisible(True)
            self.raise_()
        # 宽度伸缩：由小药丸线性扩张到完整内容宽度（内容随几何一起收放，无残留）
        w = int(self._min_w + (self._full_w - self._min_w) * value)
        self.setFixedWidth(w)
        self._place(value, w)
        self.update()

    def _place(self, value: float, w: int):
        """按屏幕可用区定位于**顶部居中**（同 CapRise 倒计时结束通知）：
        value=0 收在屏幕上方之外，value=1 静止在顶部下方 _TOP_GAP。"""
        S = QApplication.primaryScreen().availableGeometry()
        x = S.center().x() - w // 2
        y_full = S.y() + self._TOP_GAP                     # 完全展开静止位置
        hidden = S.y() - self.height() - self._TOP_GAP     # 收起位：整条滑出屏幕上方
        y = int(round(hidden + (y_full - hidden) * value))
        self.move(x, y)

    expand = Property(float, get_expand, set_expand)

    # ------------------------------------------- contentWidth / contentHeight

    def _recenter_x(self):
        """宽高动画改变宽度时保持屏幕水平居中（不动纵位）。"""
        S = QApplication.primaryScreen().availableGeometry()
        self.move(S.center().x() - self.width() // 2, self.y())

    def _get_content_width(self) -> float:
        return self.width()

    def _set_content_width(self, value: float):
        self.setFixedWidth(int(value))
        self._recenter_x()

    def _get_content_height(self) -> float:
        return self.height()

    def _set_content_height(self, value: float):
        self.setFixedHeight(int(value))
        # 高度变化时保持顶部锚定在屏幕可用区下方
        S = QApplication.primaryScreen().availableGeometry()
        self.move(self.x(), S.y() + self._TOP_GAP)

    contentWidth = Property(float, _get_content_width, _set_content_width)
    contentHeight = Property(float, _get_content_height, _set_content_height)