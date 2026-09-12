"""
胶囊通知栏 - 局域网剪切板文件投递的三段式状态展示

与 CapRise 倒计时结束通知 (TimerNoticeOverlay) 一致，是独立的系统级胶囊：
无边框 + 置顶 + Tool 的顶层窗口，透明背景，不抢焦点；显示在**屏幕顶部**，
完全脱离主窗口/同步界面，随屏幕可用区自动摆位（同 CapRise 倒计时结束通知）。

本文件包含两层：
- _CapsuleItem  : 单条胶囊（可用提示 / 传输态）。自身不做屏幕居中，水平位置由
                  协调者按“槽位”驱动（_slot_x）；几何变化时发出 layout_dirty。
- CapsuleNotification : 多胶囊协调者，对外 API 与旧版单胶囊完全一致。
    * 新可用消息：瞬态胶囊悬浮 2.5s 后自动收起；
    * 传输中有新可用：在传输胶囊**左侧**新开一条胶囊并排显示，两条整体实时居中；
      可用胶囊收起后传输胶囊平滑回中；
    * 传输胶囊永远不被“可用”顶掉；多条可用以最新替换（始终单条可用胶囊）。

表现文件投递的三个阶段：
    1. 收到通知   : “有可用的远程文件 {名称} {大小}”，提示可用 Ctrl+V 粘贴
    2. 开始传输   : 原“可用”胶囊就地过渡为传输态（同胶囊），露出文件名 + 淡蓝进度条
    3. 完成       : 进度条填满后，以对钩动画淡出隐藏

风格与动画约定（本分支自行实现）：
- 本体样式沿用常用胶囊栏的胶囊观感：系统背景派生的纵向渐变 + 发丝描边
  （深色下 #505050），非固定蓝色，不做玻璃拟态。进度条仍为淡蓝色（#74c0fc）。
- 出现 / 收起：自屏幕上方滑入，宽度由小药丸线性伸展；收起时上滑 + 淡出。
  不使用常驻透明度效果——它缓存源图，重新展开时会产生文字纵向重叠的残留。
- 完成后：圆角胶囊内播放对钩绘制动画，随后整条收起淡出。
"""
from PySide6.QtCore import (Qt, QRect, QRectF, QPoint, QSize,
                            QParallelAnimationGroup, QPropertyAnimation, Property,
                            QEasingCurve, Signal, QEvent, QByteArray, QObject, QTimer)
from PySide6.QtGui import (QPainter, QColor, QPen, QPainterPath, QLinearGradient,
                           QPalette, QFont, QFontMetrics)
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


class _RoundedProgressBar(QProgressBar):
    """胶囊进度条：paintEvent 自绘圆角轨道 + 圆角淡蓝块 + 居中文本。

    QSS 的 border-radius 对 QProgressBar 轨道在部分平台/Qt 版本下不可靠
    （轨道四角仍被填成直角矩形，空条时观感即"长方形"），改为自绘保证
    圆角从第一帧起一致。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text_color = QColor("#475569")
        # PySide6 未把 QProgressBar::textVisible 属性暴露给 Python 覆写，自维护标志
        self._text_visible = True
        self._text_align = Qt.AlignCenter   # 胶囊居中；表格行可改左对齐
        f = self.font()
        f.setPixelSize(11)
        f.setWeight(QFont.DemiBold)
        self.setFont(f)

    def setTextVisible(self, visible: bool):
        """覆写：同步自维护标志（paintEvent 不依赖 Qt 未暴露的属性）。"""
        self._text_visible = bool(visible)
        super().setTextVisible(visible)
        self.update()

    def set_text_color(self, color: str):
        """按深浅主题刷新文本颜色（深色下数值用白、标签用灰）。"""
        self._text_color = QColor(color)
        self.update()

    def set_text_alignment(self, align):
        """设置文本对齐（默认居中；表格行进度条用左对齐保持原观感）。"""
        self._text_align = align
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        rect = QRectF(0, 0, w, h)
        radius = min(h / 2.0, w / 2.0)
        # 固定圆角底板（轨道）：始终完整圆角、与进度无关 —— 空条/初始即是圆角。
        # 填充不再自绘圆角块（窄块时圆角被钳制成"左侧方形"，15% 后拉长才显圆角），
        # 改为把填充裁剪限制在底板圆角内：任何进度下整体保持圆润，稳定可靠。
        base = QPainterPath()
        base.addRoundedRect(rect, radius, radius)
        p.fillPath(base, QColor(120, 120, 120, 56))
        ratio = ((self.value() - self.minimum()) / (self.maximum() - self.minimum())
                 if self.maximum() > self.minimum() else 0.0)
        chunk_w = rect.width() * max(0.0, min(1.0, ratio))
        if chunk_w > 1.0:
            p.save()
            p.setClipPath(base)
            p.fillRect(QRectF(0, 0, chunk_w, rect.height()), QColor("#74c0fc"))
            p.restore()
        # 文本：居中，超宽省略（保持 25% · 1.0 GB / 4.0 GB 的进度文字）
        text = self.format()
        if self._text_visible and text:
            fm = QFontMetrics(self.font())
            elided = fm.elidedText(text, Qt.ElideMiddle, max(10, w - 12))
            p.setPen(self._text_color)
            p.drawText(rect, self._text_align, elided)
        p.end()


class _ElideLabel(QLabel):
    """自绘省略文本标签：PySide6 未暴露 QLabel.setElideMode，改用 QFontMetrics
    在绘制时对超宽文本做省略（标题中段省略保留扩展名、提示中段省略保留两端），
    并允许布局把宽度压到很小——超长文件名不再把胶囊撑到屏幕外。

    sizeHint 仍返回完整文本宽度（受 max_width 上限钳制），真正显示宽度由布局
    按胶囊可用空间分配；minimumSizeHint 返回 (0, 行高)，布局可随意压缩。
    """

    def __init__(self, elide: Qt.TextElideMode = Qt.ElideMiddle,
                 max_w: int = 100000, parent=None):
        super().__init__(parent)
        self._elide = elide
        self._max_w = max(40, int(max_w))

    def set_max_width(self, w: int):
        w = max(40, int(w))
        if w != self._max_w:
            self._max_w = w
            self.updateGeometry()

    def sizeHint(self):
        sh = super().sizeHint()
        return QSize(min(sh.width(), self._max_w), sh.height())

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setPen(self.palette().color(QPalette.Text))
        fm = QFontMetrics(self.font())
        elided = fm.elidedText(self.text(), self._elide, max(1, self.width()))
        p.drawText(self.rect(), int(self.alignment()), elided)
        p.end()


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


class _CapsuleItem(QFrame):
    """单条胶囊：屏幕顶部居中的独立顶层胶囊（由协调者按槽位驱动水平位置）。

    自身不计算屏幕居中——水平位置跟随协调者下发的 _slot_x；每次几何变化
    （展开/收起帧、宽度伸缩、隐藏落点）都会发出 layout_dirty，由协调者
    对整组胶囊做实时居中重排。
    """

    # 点击胶囊请求粘贴远程文件（收到通知状态下点击触发热点）
    paste_requested = Signal()
    # 几何变化（展开帧/宽度动画/隐藏）→ 协调者重排整组
    layout_dirty = Signal()

    _TOP_GAP = 40         # 距屏幕可用区顶部的静止间距（同 CapRise 倒计时结束通知）
    _ANIM_MS = 300        # 展开/收起滑动时长
    _TRANSITION_MS = 260  # 内容长度伸缩过渡时长
    _EASE = QEasingCurve.OutCubic

    def __init__(self, parent=None):
        super().__init__(parent)
        # 独立系统级通知：无边框 + 置顶 + Tool 顶层窗口，透明背景，不抢焦点。
        # 脱离任何主窗口/同步界面，位置由协调者（管理器）按整组居中下发。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)

        # 状态
        self._expand = 0.0   # 0 收起（小药丸，沉到屏幕上方之外） -> 1 完全展开
        self._full_w = 0     # 当前内容完整宽度（测量后使用）
        self._min_w = 44     # 收起态最小胶囊宽度（中心锚定药丸）
        self._slot_x = 0     # 协调者下发的水平槽位（整组居中后的左边缘）
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
        self._queue_waiting = 0      # 传输态排队等待数（提示“等待”）
        self._transfer_hint = ""     # 传输态基础提示（不含排队后缀）
        self._total_bytes = 0        # 进度分母锁定：begin_transfer 时定为通知元数据大小
        self._hint_mode = False      # 提示态（纯文本，如“无效的粘贴位置”）：不可点击
        self._transferring = False   # 传输态状态位（进度条可能因胶囊过窄暂隐，状态判断以此为准）
        self._bar_fit_w = 0          # 进度条完整显示所需的最小胶囊宽度（内容测量后使用）
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

        # 系列标签：左侧“投递”标识（所有投递相关胶囊共有，常驻显示，
        # 与进度条同色系，一眼识别这是投递系列的通知胶囊）
        self._tag = QLabel("投递")
        self._tag.setAlignment(Qt.AlignCenter)
        self._tag.setFixedHeight(20)
        # Fixed：横向永不拉伸——接管短名文件后胶囊内出现多余空间时，
        # 不得把"投递"标识拉长来填充右侧空缺
        self._tag.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self._tag.setStyleSheet(
            "background-color: #74c0fc; color: #ffffff;"
            "border-radius: 10px; padding: 0 10px;"
            "font-size: 11px; font-weight: 600; letter-spacing: 1px;")
        inner.addWidget(self._tag, 0, Qt.AlignVCenter)

        # 左侧状态图标区（对钩仅在完成态显示）
        self._icon = _CheckMark(28)
        self._icon.setVisible(False)
        inner.addWidget(self._icon, 0, Qt.AlignVCenter)

        # 文本内容区：整体作为一个可垂直居中的子控件
        self._text_box = QWidget()
        text_col = QVBoxLayout(self._text_box)
        text_col.setSpacing(3)
        text_col.setContentsMargins(0, 0, 0, 0)

        # 标题：中段省略，且 sizeHint 上限 320px——长文件名不再把胶囊撑到接近
        # 屏幕宽（此前宽屏下会完整显示整个长名），一律压缩显示、保留首尾
        self._title = _ElideLabel(Qt.ElideMiddle, max_w=320)
        self._title.setTextInteractionFlags(Qt.NoTextInteraction)
        text_col.addWidget(self._title)

        # 提示：中段省略（保留"正在投递 X"与"N 等待"两端），"N 等待"不被压掉
        self._hint = _ElideLabel(Qt.ElideMiddle)
        self._hint.setVisible(False)
        text_col.addWidget(self._hint)

        inner.addWidget(self._text_box, 0, Qt.AlignVCenter)

        # 传输进度条：显示「百分比 + 已接收/总量」（传输态出现），自绘圆角
        self._bar = _RoundedProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setTextVisible(True)
        self._bar.setFixedWidth(280)
        self._bar.setFixedHeight(20)
        self._bar.set_text_color("#94a3b8")
        self._bar.setVisible(False)
        inner.addWidget(self._bar, 0, Qt.AlignVCenter)

        outer.addWidget(self._content)

    def show_available(self, file_name: str, total_bytes: int):
        """状态1 —— 收到通知：提示“有可用的远程文件 {名称} {大小}”。"""
        self._stop_all()
        self._refresh_theme()
        self._hint_mode = False
        self._title.setText(f"有可用的远程文件  {file_name}  {format_bytes(total_bytes)}")
        self._hint.setText("按 Ctrl+V 粘贴到此设备")
        self._hint.setVisible(True)
        self._bar.setVisible(False)
        self._bar.setValue(0)
        self._icon.setVisible(False)
        self._queue_waiting = 0
        self._transferring = False
        self._present()

    def show_hint(self, text: str):
        """提示态：显示纯文本（如“无效的粘贴位置”），无进度条/对钩，暂不响应点击。

        与 show_available 同生命周期（协调者 _HINT_MS=1s 后自动收起），用于轻量
        toast 式反馈，不打断用户操作。
        """
        self._stop_all()
        self._refresh_theme()
        self._hint_mode = True
        self._title.setText(text)
        self._hint.setVisible(False)
        self._bar.setVisible(False)
        self._bar.setValue(0)
        self._icon.setVisible(False)
        self._queue_waiting = 0
        self._transferring = False
        self._present()

    def begin_transfer(self, file_name: str, total_bytes: int):
        """状态2 —— 开始传输：露出文件名 + 淡蓝进度条（从 0 起步）。

        进度分母锁定为通知元数据大小（_total_bytes），之后 set_progress 一律
        以它为 total（文件在复制时已完整，与 FILE_BEGIN 真实大小一致）。
        """
        self._stop_all()
        self._refresh_theme()
        self._hint_mode = False
        self._total_bytes = int(total_bytes)
        self._title.setText(file_name)
        self._hint.setText(f"正在投递  {format_bytes(self._total_bytes)}")
        self._hint.setVisible(True)
        self._bar.setVisible(True)
        self._bar.setEnabled(True)
        self._bar.setValue(0)
        self._bar.setFormat(f"0%  ·  0 B / {format_bytes(self._total_bytes)}")
        self._icon.setVisible(False)
        self._queue_waiting = 0
        self._transfer_hint = f"正在投递  {format_bytes(self._total_bytes)}"
        self._transferring = True
        self._present()
        self._sync_bar_visibility()

    def set_progress(self, received: int, total: int):
        """更新传输进度（received / total），同步显示百分比与已接收/总量。

        分母以 begin_transfer 锁定的 _total_bytes（通知元数据大小）为主；文件在
        复制时已完整，信号 total（FILE_BEGIN 真实大小）与之相等，仅作兜底跟进，
        不回跳。跟进后同步更新"正在投递 X"提示为真实总量。
        """
        self._bar.setEnabled(True)
        received = max(0, int(received))
        total_arg = int(total)
        if self._total_bytes > 0:
            denominator = max(self._total_bytes, total_arg, received)
        else:
            denominator = total_arg
        # 学到比锁定的元数据更大的真实总量：分母跟进，并同步刷新"正在投递 X"
        if denominator > self._total_bytes:
            self._total_bytes = denominator
            self._transfer_hint = f"正在投递  {format_bytes(denominator)}"
            if self._queue_waiting == 0:
                self._hint.setText(self._transfer_hint)
        percent = int(received / denominator * 100) if denominator > 0 else 0
        self._bar.setValue(max(0, min(1000, int(received / denominator * 1000)
                                      if denominator > 0 else 0)))
        if denominator > 0:
            self._bar.setFormat(
                f"{percent}%  ·  {format_bytes(received)} / {format_bytes(denominator)}")
        self._sync_bar_visibility()   # 胶囊宽度足够时显示进度条，不足时保持隐藏

    def is_transferring(self) -> bool:
        """胶囊当前是否处于传输态（以状态位为准；进度条可能因胶囊过窄暂隐）。"""
        return self.isVisible() and self._transferring

    def set_queue_waiting(self, count: int):
        """传输态下显示排队等待数（“{count} 等待”）；非传输态忽略。

        胶囊宽度随内容自适应：已完全展开则做长度伸缩动画；
        展开动画进行中只更新完整宽度目标，让在途动画按新宽度走到底，避免回弹。
        """
        if not self.isVisible() or not self.is_transferring():
            return
        count = max(0, int(count))
        if count == self._queue_waiting:
            return
        self._queue_waiting = count
        self._hint.setText(self._transfer_hint if count == 0
                           else f"{self._transfer_hint}  ·  {count} 等待")
        if self._expand >= 0.99:
            self._present()
        else:
            w, _ = self._content_size()
            self._full_w = w

    def complete(self):
        """状态3 —— 完成：进度封顶，播放对钩动画后淡出隐藏。"""
        if not self.isVisible():
            return
        self._bar.setValue(1000)
        self._bar.setVisible(False)
        self._hint.setVisible(False)
        self._icon.setVisible(True)
        self._queue_waiting = 0
        self._transferring = False
        # 完成态收起提示与进度条，内容整体淡入 + 长度收缩；高度保持不变，
        # 避免收起进度条/提示后胶囊高度被重新测量而明显变窄
        self._present(keep_height=True)
        self._check_anim.stop()
        self._check_anim.setStartValue(0.0)
        self._check_anim.setEndValue(1.0)
        self._check_anim.start()

    def dismiss(self):
        """手动收起（动画）"""
        self._hide_anim()

    def mouseReleaseEvent(self, event):
        """点击胶囊（收到通知态）请求粘贴远程文件；提示态/传输态不响应点击。"""
        if self._hint_mode:
            return
        if event.button() == Qt.LeftButton and not self._transferring:
            self.paste_requested.emit()
        super().mouseReleaseEvent(event)

    # ---------------------------------------------------------------- 动画

    def _refresh_measure(self):
        """递归失效嵌套布局与子控件尺寸缓存，使 _content_size 量出最新内容。

        Qt 的 QWidgetItem 对子控件尺寸有独立缓存：仅对顶层布局 invalidate()
        不会刷新它——接管时（长名→短名）inner 仍读到 text_box 的旧长文本宽度，
        导致胶囊宽度不收缩、多余空间把左侧"投递"标签拉伸；等待后缀同理会量出
        过小宽度而把"N 等待"挤出。这里对每个子控件补 updateGeometry()（刷新其
        QWidgetItem 缓存）并递归失效所有嵌套布局。
        """

        def refresh_layout(lay):
            for i in range(lay.count()):
                item = lay.itemAt(i)
                wdg = item.widget()
                if wdg is not None:
                    wdg.updateGeometry()
                    refresh_widget(wdg)
                elif item.layout() is not None:
                    refresh_layout(item.layout())
            lay.invalidate()

        def refresh_widget(w):
            lay = w.layout()
            if lay is None:
                w.updateGeometry()
            else:
                refresh_layout(lay)

        refresh_widget(self._content)

    def _content_size(self):
        """当前内容的完整尺寸（宽/高），作为长度伸缩的目标。

        收起/hide 后子部件 sizeHint 会缓存为空内容，重新展开前必须先
        失效并激活布局，否则量出过小尺寸导致内容溢出/文字重叠。
        传输态测量始终计入进度条（即使当前因胶囊过窄暂隐），并顺带刷新
        _bar_fit_w（进度条不溢出的最小胶囊宽度）；最终宽度受屏幕上限钳制，
        超长文件名省略显示而不把胶囊撑到屏幕外。
        """
        c = self._content
        bar_vis = self._bar.isVisible()
        if self._transferring and not bar_vis:
            self._bar.setVisible(True)     # 传输态测量必须包含进度条
        self._refresh_measure()
        lay = c.layout()
        lay.activate()
        sh = lay.sizeHint()
        if self._transferring:
            self._bar_fit_w = lay.minimumSize().width()
            if not bar_vis:
                self._bar.setVisible(False)   # 还原隐藏（胶囊过窄期间不显示进度条）
        w = min(sh.width(), self._screen_max_w())
        return max(self._min_w, w), max(40, sh.height())

    def _screen_max_w(self) -> int:
        """胶囊最大宽度：屏幕可用宽度左右各留 24px 边距（超长文件名省略显示，
        进度条不被挤出屏幕右缘）。"""
        S = QApplication.primaryScreen().availableGeometry()
        return max(self._min_w, S.width() - 48)

    def _sync_bar_visibility(self):
        """按当前胶囊宽度决定进度条显隐：宽度不足以完整容纳进度条时隐藏，
        避免进度条右缘超出胶囊被裁剪（展开/伸缩动画中间态的关键修复）。"""
        if not self._transferring or self._bar_fit_w <= 0:
            return
        if self.width() >= self._bar_fit_w:
            if not self._bar.isVisible():
                self._bar.setVisible(True)
        elif self._bar.isVisible():
            self._bar.setVisible(False)

    def _present(self, keep_height: bool = False):
        """内容就位后统一呈现：已完全展开则长度伸缩，否则走展开动画。

        Args:
            keep_height: True 时保持当前高度不变（完成态收起进度条/提示后，
                内容高度会变小，但胶囊高度不应随之变窄），仅伸缩宽度。
        """
        if self.isVisible() and self._expand >= 0.99:
            w, h = self._content_size()
            self._full_w = w
            self._resize_to(w, self.height() if keep_height else h)
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
        self._park_above()                   # 先摆到屏幕上方之外（收起位），避免首帧闪现
        self.setVisible(True)
        self.raise_()
        w, h = self._content_size()
        self._full_w = w
        self.setFixedHeight(h)
        self._sync_bar_visibility()   # 起手宽度（小药丸）不足以容纳进度条 → 先隐藏
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
            self.layout_dirty.emit()   # 本胶囊已消失 → 协调者重排剩余胶囊回中

    def _reset_content(self):
        """清空提示/标题/进度条/对钩，回到初始空白状态。"""
        self._title.clear()
        self._hint.clear()
        self._hint.setVisible(False)
        self._bar.setVisible(False)
        self._bar.setValue(0)
        self._icon.setVisible(False)
        self._queue_waiting = 0
        self._transferring = False

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
        """胶囊进度条主题化：自绘圆角条（轨道+块不依赖 QSS），此处只换文本颜色。"""
        self._bar.set_text_color(text_color)

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
        self._sync_bar_visibility()   # 宽度足够容纳进度条前保持隐藏
        self._apply_slot()
        self.layout_dirty.emit()   # 几何变化 → 协调者实时居中整组
        self.update()

    def apply_slot(self, x: int):
        """协调者下发的水平槽位：整组居中后的本胶囊左边缘。"""
        self._slot_x = int(x)
        self._apply_slot()

    def _apply_slot(self):
        """按当前 expand 摆放：水平用 _slot_x，纵向由展开度滑入/滑出。"""
        S = QApplication.primaryScreen().availableGeometry()
        y_full = S.y() + self._TOP_GAP                     # 完全展开静止位置
        hidden = S.y() - self.height() - self._TOP_GAP     # 收起位：整条滑出屏幕上方
        y = int(round(hidden + (y_full - hidden) * self._expand))
        self.move(self._slot_x, y)

    def _park_above(self):
        """显示前先停在屏幕上方之外（收起位），避免首帧闪现。"""
        S = QApplication.primaryScreen().availableGeometry()
        self.move(self._slot_x, S.y() - self.height() - self._TOP_GAP)

    expand = Property(float, get_expand, set_expand)

    # ------------------------------------------- contentWidth / contentHeight

    def _get_content_width(self) -> float:
        return self.width()

    def _set_content_width(self, value: float):
        self.setFixedWidth(int(value))
        self._sync_bar_visibility()   # 长度伸缩动画中间态同样按宽度控制进度条显隐
        self.layout_dirty.emit()   # 宽度伸缩中 → 协调者实时居中整组

    def _get_content_height(self) -> float:
        return self.height()

    def _set_content_height(self, value: float):
        self.setFixedHeight(int(value))
        # 高度变化时保持顶部锚定在屏幕可用区下方
        S = QApplication.primaryScreen().availableGeometry()
        self.move(self.x(), S.y() + self._TOP_GAP)

    contentWidth = Property(float, _get_content_width, _set_content_width)
    contentHeight = Property(float, _get_content_height, _set_content_height)


class CapsuleNotification(QObject):
    """多胶囊协调者：可用提示（瞬态）在左、传输胶囊在右，整组实时居中。

    对外 API 与旧版单胶囊完全一致，sync_window / DropZone 无需改动：
    show_available / begin_transfer / set_progress / is_transferring /
    set_queue_waiting / complete / dismiss / isVisible / frameGeometry / close。

    行为：
    - 新可用消息：独立胶囊悬浮 _AVAIL_MS(2.5s) 后自动收起；传输中则在传输胶囊左侧
      并排显示，收起后传输胶囊随整组重排平滑回中。
    - 可用胶囊在场时开始传输：同一胶囊就地过渡为传输态（同胶囊过渡），不另起新
      传输胶囊；换位后备用胶囊承担新的“可用”角色。
    - 传输胶囊永远不被“可用”顶掉；多条可用以最新替换（始终单条可用胶囊）。
    - 点击可用胶囊 → 收起该瞬态胶囊并发出 paste_requested（与旧版点击触发热点一致）。
    """

    paste_requested = Signal()
    # 瞬态"可用远程文件"胶囊已收起/隐藏（超时、点击或被动 dismiss 均触发）：
    # 调用方（sync_window）据此释放系统级 Ctrl+V 劫持
    available_hidden = Signal()

    _AVAIL_MS = 2500   # 新可用消息悬浮时长（到时自动收起）
    _HINT_MS = 1000    # 提示态（如"无效的粘贴位置"）悬浮时长，短于可用消息，轻量 toast 即时收起
    _GAP = 14          # 并排胶囊间距

    def __init__(self, parent=None):
        super().__init__(parent)
        self._available = _CapsuleItem()   # 瞬态"有可用文件"提示（最新为主，最多一条）
        self._transfer = _CapsuleItem()    # 持久传输胶囊（进度/排队/对钩）
        # 两条胶囊共用角色信号：同胶囊过渡时会换位（可用↔传输），点击/隐藏
        # 广播一律以"触发者是否仍为可用角色"为准（sender 守卫），换位无需重接线
        for cap in (self._available, self._transfer):
            cap.paste_requested.connect(self._on_available_clicked)
            cap.layout_dirty.connect(self._relayout)
            cap.layout_dirty.connect(self._on_available_layout_dirty)
        self._avail_timer = QTimer(self)
        self._avail_timer.setSingleShot(True)
        self._avail_timer.timeout.connect(self._on_avail_timeout)
        # 提示态（如"无效的粘贴位置"）期间新可用消息不得覆盖提示
        self._hint_active = False
        # 当前"可用"胶囊代表的会话 id（与 begin_transfer 比对，决定是否同胶囊过渡）
        self._available_session = None

    # ------------------------------------------------------------ 对外 API

    def show_available(self, file_name: str, total_bytes: int, session_id=None):
        """新可用消息：瞬态胶囊悬浮 2.5s 后自动收起（最新为主，重启计时）。

        提示态（如"无效的粘贴位置"）期间不覆盖提示，避免无效提示被新到的
        可用消息遮挡；提示收起后才恢复正常。
        """
        if self._hint_active:
            return
        self._avail_timer.stop()
        self._available_session = session_id
        self._available.show_available(file_name, total_bytes)
        self._relayout()
        self._avail_timer.start(self._AVAIL_MS)

    def show_hint(self, text: str):
        """轻提示态：瞬态胶囊显示纯文本（如"无效的粘贴位置"），1s 后自动收起。

        提示态优先于可用消息：顶掉当前"可用远程文件"提示（含其剩余计时），
        期间新到的可用消息不再覆盖提示，直到提示收起。专用 _HINT_MS 计时，
        比可用消息（_AVAIL_MS）更短，保证无效提示即时报错即走。
        """
        self._hint_active = True
        self._avail_timer.stop()
        self._available_session = None
        self._available.show_hint(text)
        self._relayout()
        self._avail_timer.start(self._HINT_MS)

    def begin_transfer(self, file_name: str, total_bytes: int, session_id=None):
        """开始传输：优先让"可用"胶囊就地过渡为传输态（同胶囊过渡）。

        可用提示本身即本次粘贴消费的会话，无需额外弹出一条传输胶囊：
        内容（标题/提示/进度条）在原胶囊内平滑切换，而不是收起可用、另起
        一条传输胶囊。仅当没有可用胶囊悬浮（可用超时后的热键粘贴、排队接管
        时可用已收起），或可用胶囊属于其他会话（排队接管时更新的可用提示仍
        在等待粘贴）时，才由独立传输胶囊呈现。
        """
        self._hint_active = False
        self._avail_timer.stop()
        if (self._available.isVisible()
                and (session_id is None or session_id == self._available_session)):
            # 同胶囊过渡：可用胶囊就地转为传输态；原传输胶囊换位成新的"可用"
            # 待用，并收起旧完成态，避免两条胶囊同时悬浮
            self._available.begin_transfer(file_name, total_bytes)
            spare = self._transfer
            self._available, self._transfer = self._transfer, self._available
            spare.dismiss()
            self._available_session = None
        else:
            self._transfer.begin_transfer(file_name, total_bytes)
        self._relayout()

    def set_progress(self, received: int, total: int):
        self._transfer.set_progress(received, total)

    def is_transferring(self) -> bool:
        """当前是否有传输胶囊处于传输态（进度条可见）。"""
        return self._transfer.is_transferring()

    def set_queue_waiting(self, count: int):
        self._transfer.set_queue_waiting(count)

    def complete(self):
        self._transfer.complete()

    def dismiss(self):
        """手动收起：优先收掉瞬态可用胶囊；无可用时收传输胶囊。"""
        self._hint_active = False
        if self._available.isVisible():
            self._available_session = None
            self._available.dismiss()
        else:
            self._transfer.dismiss()
        self._avail_timer.stop()

    def isVisible(self) -> bool:
        return self._available.isVisible() or self._transfer.isVisible()

    def available_visible(self) -> bool:
        """瞬态"可用远程文件"胶囊是否悬浮中（尚未被传输消费/收起）。"""
        return self._available.isVisible()

    def frameGeometry(self):
        """整组可见胶囊的包围矩形（DropZone 避让用，与旧版语义一致）。"""
        rect = None
        for cap in (self._available, self._transfer):
            if cap.isVisible():
                rect = cap.frameGeometry() if rect is None else rect.united(cap.frameGeometry())
        if rect is None:
            S = QApplication.primaryScreen().availableGeometry()
            rect = QRect(S.center().x() - 5, S.y() + self._transfer._TOP_GAP, 10, 1)
        return rect

    def close(self):
        self._avail_timer.stop()
        self._hint_active = False
        self._available.close()
        self._transfer.close()

    # ------------------------------------------------------------ 内部

    def _on_available_clicked(self):
        """点击瞬态可用胶囊：收起并请求粘贴（上游失败时 dismiss 再次收起为幂等）。

        两条胶囊共用 paste_requested，同胶囊过渡换位后触发者不再是"可用"
        角色（传输态进度条可见时也不发射点击），sender 守卫保证只响应可用胶囊。
        """
        if self.sender() is not self._available:
            return
        self._hint_active = False
        self._avail_timer.stop()
        self._available_session = None
        self._available.dismiss()
        self.paste_requested.emit()

    def _on_avail_timeout(self):
        self._hint_active = False
        self._available_session = None
        self._available.dismiss()

    def _on_available_layout_dirty(self):
        """可用胶囊几何变化：仅在其真正隐藏（收起动画到落点）后广播 available_hidden。

        layout_dirty 在展开/收起每一帧都会触发，此处只认"已隐藏"状态，
        供 sync_window 释放系统级 Ctrl+V 劫持（重复广播无副作用）。
        """
        if self.sender() is not self._available:
            return
        if not self._available.isVisible():
            self.available_hidden.emit()

    def _relayout(self):
        """按各胶囊当前宽度整组水平居中：可用在左、传输在右。

        任一胶囊展开/收起/伸缩的每一帧都会触发本方法（见 layout_dirty），
        直接下发槽位完成“实时居中”：
        - 可用胶囊滑入时宽度渐增 → 传输胶囊逐帧左移腾位；
        - 可用胶囊收起时宽度渐减 → 传输胶囊逐帧右移回中；
        - 仅剩单条时即自身居中（与旧版一致）。
        """
        caps = [c for c in (self._available, self._transfer)
                if c.isVisible() and c._expand > 0.001]
        if not caps:
            return
        S = QApplication.primaryScreen().availableGeometry()
        total_w = sum(c.width() for c in caps) + self._GAP * (len(caps) - 1)
        x = S.center().x() - total_w // 2
        for cap in caps:
            cap.apply_slot(x)
            x += cap.width() + self._GAP