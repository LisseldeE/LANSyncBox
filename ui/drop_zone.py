"""
顶部快捷放置区（快捷添加文件） - 独立置顶 drop-target 放置条

同步房间运行期间，用户在桌面任意位置按住左键拖动物体并移到屏幕顶部时，一条顶部
胶囊放置条滑入（带过渡动画）；在条上松手且确为本地文件 → 加入当前同步列表
（根目录/当前目录）。

实现：与上一版「全屏 + WS_EX_TRANSPARENT 穿透覆盖层」不同：
- 全屏 + 点击穿透的窗口无法被系统 OLE 文件拖放命中（WS_EX_TRANSPARENT 使窗口在
  鼠标命中测试中被穿透，Explorer 的 OLE DoDragDrop 路由不到它），所以上一版放置条从不出现。
- 本版改为**独立、普通（非穿透）置顶小窗口**，本身是有效的 OLE drop-target，可真实收到文件；
  是否弹条改由 Win32 全局钩子检测（ui.drag_detector.DragDetector）驱动，用 Qt 定时器轮询。

胶囊共存：若关联胶囊正在顶部悬浮，放置条定位在其下方，避免两条浮条重叠。
"""
from PySide6.QtCore import Qt, QRectF, QPoint, QVariantAnimation, QEasingCurve, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QFontMetrics, QLinearGradient, QPalette
from PySide6.QtWidgets import QApplication, QWidget, QSizePolicy

_TOP_GAP = 40       # 距屏幕可用区顶部的静止间距（同胶囊）
_ANIM_MS = 300      # 放置条滑入/滑出时长
_VERT_BAND = 120    # 触发弹条的顶部垂直带（屏顶起算的判定高度）


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


class _Strip(QWidget):
    """放置条本体：一块胶囊，自绘胶囊样式 + 加号图标 + 提示文字。"""

    _H = 56          # 高度

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text = "松开以添加文件到同步列表"
        self._hovered = False
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_text(self, text: str):
        self._text = text
        self.update()

    def set_hovered(self, on: bool):
        if on != self._hovered:
            self._hovered = on
            self.update()

    def content_width(self) -> int:
        """按当前文字量出所需宽度（含左侧加号与内边距）。"""
        fm = QFontMetrics(self.font())
        tw = fm.horizontalAdvance(self._text)
        return max(140, tw + 22 + 20 * 2)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        # 胶囊底：系统背景派生的纵向渐变 + 发丝描边（同胶囊，深色下 #505050）
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

        border = QColor("#74c0fc") if self._hovered else QColor(80, 80, 80, 255)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(border, 1))
        p.drawRoundedRect(
            QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5),
            radius - 0.5, radius - 0.5)

        # 左侧加号（几何描边图标）
        cx = int(rect.x() + 20 + 11)
        cy = int(rect.center().y())
        accent = QColor("#74c0fc") if dark or self._hovered else QColor("#3b82f6")
        pen = QPen(accent, 2)
        pen.setCapStyle(Qt.RoundCap)
        h = 16
        p.setPen(pen)
        p.drawLine(QPoint(cx, cy - h // 2), QPoint(cx, cy + h // 2))
        p.drawLine(QPoint(cx - h // 2, cy), QPoint(cx + h // 2, cy))

        # 提示文字（深浅主题自适应）
        txt = QColor("#ffffff") if dark else QColor("#1c2733")
        p.setPen(txt)
        tf = self.font()
        tf.setPixelSize(13)
        tf.setBold(True)
        fm = QFontMetrics(tf)
        p.setFont(tf)
        txt_x = rect.x() + 20 + 22 + 10
        p.drawText(QPoint(txt_x, cy + fm.ascent() // 2), self._text)
        p.end()


class DropZone(QWidget):
    """顶部快捷放置条：独立置顶、非穿透 OLE drop-target。

    使用：房间就绪后构造并 show()，再在 closeEvent 里 close()。由 Qt 定时器调用
    pump(detector) 轮询全局钩子状态以决定滑入/滑出。
    """

    # 全部文件放到当前同步目录(根)后发出：path 列表已完成添加
    files_added = Signal(list)

    def __init__(self, add_callback=None, capsule=None):
        super().__init__()
        # 独立顶层条：置顶、无边框、Tool（不抢焦点）；作为 OLE drop-target，绝不设穿透。
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAcceptDrops(True)

        self._add_callback = add_callback or (lambda paths: None)
        self._capsule = capsule

        # 放置条本体
        self._strip = _Strip(self)
        self._strip.hide()

        # 滑入/滑出动画
        self._reveal = QVariantAnimation(self)
        self._reveal.setDuration(_ANIM_MS)
        self._reveal.setEasingCurve(QEasingCurve.OutCubic)
        self._reveal.setStartValue(0.0)
        self._reveal.setEndValue(1.0)
        self._reveal.valueChanged.connect(self._apply_reveal)

        self._min_w = 140
        self._full_w = self._min_w
        self._is_revealed = False

        # 窗口自身很小；默认摆到屏顶居中（隐藏态在屏幕外）。
        self.setFixedSize(140, _Strip._H)
        self._layout_hidden()

    # ------------------------------------------------------------- 对外接口
    def set_add_callback(self, fn):
        self._add_callback = fn or (lambda paths: None)

    def set_capsule(self, capsule):
        self._capsule = capsule

    # ------------------------------------------------------------- 定位
    def _screen(self):
        return QApplication.primaryScreen().availableGeometry()

    def _obstacle_y(self) -> int:
        """放置条静止纵位：若有胶囊悬浮，放其下方；否则放屏顶。"""
        S = self._screen()
        cap = self._capsule
        if cap is not None and cap.isVisible():
            return cap.frameGeometry().bottom() + 14
        return S.y() + _TOP_GAP

    def _layout_hidden(self):
        """把条摆到屏幕外（隐藏态），避免影响布局。"""
        S = self._screen()
        self.move(S.center().x() - self.width() // 2, S.y() - self.height() - 40)

    def _set_pos(self, x: int, y: int, w: int, h: int):
        """把放置条本体定位并同步 strip 填满窗口。"""
        self.setMinimumSize(0, 0)
        self.setMaximumSize(w, h)
        self.setFixedSize(w, h)
        self._strip.setGeometry(0, 0, w, h)
        self.move(x, y)

    def _apply_reveal(self, value: float):
        w = int(self._min_w + (self._full_w - self._min_w) * value)
        h = self._strip._H
        S = self._screen()
        x = S.center().x() - w // 2
        rest_y = self._obstacle_y()
        hidden_y = S.y() - h - 40
        y = int(round(hidden_y + (rest_y - hidden_y) * value))
        start_x = x  # 水平居中，仅纵向滑入/滑出
        self._set_pos(start_x, y, w, h)
        if value <= 0.01:
            self._strip.hide()
        else:
            self._strip.show()

    # ------------------------------------------------------------- 轮询驱动
    def pump(self, detector):
        """由 Qt 定时器(~30ms)轮询全局钩子状态，驱动条滑入/滑出。"""
        dragging = detector.is_dragging()
        if dragging:
            cursor = detector.cursor_pos()
            in_band = cursor.y() <= self._screen().y() + _VERT_BAND
            if in_band and not self._is_revealed:
                self._show_strip(0)      # 数量未知，使用通用文案
            elif not in_band and self._is_revealed:
                self._hide_strip()
        else:
            if self._is_revealed:
                self._hide_strip()

    # ------------------------------------------------------------- 拖拽
    def _has_local_files(self, event) -> bool:
        return bool(_local_file_paths(event.mimeData()))

    def _in_strip(self, event) -> bool:
        pos = event.position().toPoint()
        # strip 填满窗口（位于 0,0），事件坐标相对窗口，落在窗口内即落在条上。
        return self.rect().contains(pos)

    def dragEnterEvent(self, event):
        if self._has_local_files(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if self._has_local_files(event):
            over = self._in_strip(event)
            if over:
                event.acceptProposedAction()
            else:
                event.ignore()
            self._strip.set_hovered(over)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._strip.set_hovered(False)

    def dropEvent(self, event):
        if not self._in_strip(event):
            self._hide_strip()
            event.ignore()
            return
        paths = _local_file_paths(event.mimeData())
        if paths:
            self._add_callback(paths)
            self.files_added.emit(list(paths))
        event.acceptProposedAction()
        self._hide_strip()

    # ------------------------------------------------------------- 动画
    def _show_strip(self, count: int):
        if count > 1:
            self._strip.set_text(f"松开以添加 {count} 个文件到同步列表")
        else:
            self._strip.set_text("松开以添加文件到同步列表")
        self._full_w = self._strip.content_width()
        self._min_w = self._full_w     # 整体弹入，不做小药丸伸缩
        self._is_revealed = True
        self._reveal.stop()
        self._reveal.setStartValue(self._reveal.currentValue() or 0.0)
        self._reveal.setEndValue(1.0)
        self._reveal.start()

    def _hide_strip(self):
        self._strip.set_hovered(False)
        self._is_revealed = False
        self._reveal.stop()
        self._reveal.setStartValue(self._reveal.currentValue() or 0.0)
        self._reveal.setEndValue(0.0)
        self._reveal.start()

    # ------------------------------------------------------------- 原生
    def showEvent(self, event):
        super().showEvent(event)
        self._layout_hidden()