"""
手机访问浮层
开关局域网网页服务、展示二维码与访问地址
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import secrets
import threading
import time

from PySide6.QtCore import (Qt, QEvent, QTimer, QRect, QRectF, QPropertyAnimation,
                            QEasingCurve, QPoint, QSize,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QPixmap, QPainter, QPalette, QPen, QRegion
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QComboBox, QWidget, QSizePolicy, QGraphicsOpacityEffect,
)

from config import Config
from i18n import I18n
from ui.widgets import ToggleSwitch


def _qr_pixmap(url: str, size: int = 168) -> QPixmap:
    """把访问地址渲染成二维码（qrcode 输出 SVG，不依赖 Pillow）。"""
    import qrcode
    import qrcode.image.svg

    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage,
                        box_size=10, border=2)
    data = image.to_string()
    if isinstance(data, str):
        data = data.encode('utf-8')
    renderer = QSvgRenderer(bytearray(data))
    pix = QPixmap(size, size)
    pix.fill(Qt.white)
    painter = QPainter(pix)
    renderer.render(painter)
    painter.end()
    return pix


class _CardFrame(QFrame):
    """浮层底板：可动画高度的圆角面板。

    控件始终保持完整高度（布局不重排，文字不会跳动），只把绘制与子控件
    裁剪到「当前可见高度」，从而做出展开/收起。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._visible_h = 0.0

    def visible_h(self) -> float:
        return self._visible_h

    def set_visible_h(self, h: float):
        h = max(1.0, float(h))
        if abs(h - self._visible_h) > 0.5:
            self._visible_h = h
            self.update()

    def changeEvent(self, event):
        # 底板用 palette 现画，主题切换时重绘
        if event.type() == QEvent.PaletteChange:
            self.update()
        super().changeEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        h = min(self._visible_h, float(self.height()))
        rect = QRectF(0.5, 0.5, self.width() - 1.0, h - 1.0)
        painter.setPen(QPen(self.palette().color(QPalette.Mid), 1))
        painter.setBrush(self.palette().color(QPalette.Window))
        painter.drawRoundedRect(rect, 10, 10)


class MobileAccessCard(QFrame):
    """「手机访问」浮动卡片。"""

    # 后台启动结果：(序号, service, info)；失败时 service 为 None
    start_finished = Signal(int, object, object)
    # 服务运行状态变化：开启成功为 True，关闭/失败为 False（驱动顶栏手机图标变色）
    running_changed = Signal(bool)

    _ANIM_MS = 180            # 出现/收起时长（与投递设置浮层一致）
    _EXPAND_MS = 220          # 二维码区展开/收起时长
    _REVEAL_MS = 160          # 展开区内容淡入时长
    _REVEAL_DELAY = 70        # 先展开、再淡入的间隔
    _FULL_W = 320
    _RISE = 12                # 出现/收起时的位移距离（同投递设置）

    def __init__(self, owner):
        super().__init__(owner, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self._owner = owner
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedWidth(self._FULL_W)

        self._service = None
        self._url_info = None
        self._starting = False       # 后台启动中
        self._want_running = False   # 用户是否仍希望服务开启
        self._start_seq = 0          # 启动批次：丢弃过期的启动结果
        self._anchor_right = 0
        self._anchor_top = 0
        self._closing = False
        self._anim = None
        self._fade = None              # 窗口透明度动画
        self._body_anim = None         # 展开区高度动画
        self._reveal_anim = None       # 展开区内容淡入淡出
        self._state_seq = 0            # 形态切换批次：丢弃过期的延迟回调
        self._expanded = False         # 当前是否展开（含启动中）
        self._outside_filter = False
        # 访问口令跨「开关重开」保持不变：已扫码的页面在服务重启后仍能用新校验码进入
        self._access_token = secrets.token_urlsafe(24)

        self._card = _CardFrame(self)
        self._card.setObjectName('mobileAccessCard')

        self._build_ui()
        self._measure()
        self._card.setGeometry(0, 0, self._FULL_W, int(self._full_h))
        self._set_body(self._compact_h)
        self.resize(self._FULL_W, int(round(self._compact_h)))

        self._poll = QTimer(self)
        self._poll.setInterval(2000)
        self._poll.timeout.connect(self._refresh_status)
        self.start_finished.connect(self._on_start_finished)

    # ---------- 界面 ----------

    def _build_ui(self):
        dark = self._is_dark()
        muted = '#a0a0a0' if dark else '#868e96'
        lay = QVBoxLayout(self._card)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(12)

        # 标题：与「投递设置」浮层同款小号灰字
        self._title = QLabel(I18n.tr('mobile_access'), self._card)
        self._title.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        lay.addWidget(self._title)

        # 开关行：复用投递设置面板的开关行排版（0/4 内边距 + 12 间距）
        self._row = QWidget(self._card)
        row = QHBoxLayout(self._row)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(12)
        switch_label = QLabel(I18n.tr('mobile_access_switch'))
        row.addWidget(switch_label)
        row.addStretch()
        self._switch = ToggleSwitch()
        self._switch.stateChanged.connect(self._on_switch)
        row.addWidget(self._switch)
        lay.addWidget(self._row)

        # 展开区：状态、二维码、地址、说明。收起时整体被裁剪并淡出
        self._reveal = QWidget(self._card)
        reveal = QVBoxLayout(self._reveal)
        reveal.setContentsMargins(0, 0, 0, 0)
        reveal.setSpacing(12)

        self._status = QLabel(I18n.tr('mobile_access_off'), self._reveal)
        self._status.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        reveal.addWidget(self._status)

        self._access = QWidget(self._reveal)
        access_lay = QVBoxLayout(self._access)
        access_lay.setContentsMargins(0, 0, 0, 0)
        access_lay.setSpacing(8)

        # 二维码白底圆角容器：深浅色主题下扫码对比度一致
        qr_box = QFrame(self._access)
        qr_box.setObjectName('qrBox')
        qr_box.setStyleSheet(
            "QFrame#qrBox { background: #ffffff;"
            " border: 1px solid palette(mid); border-radius: 10px; }"
        )
        qr_lay = QVBoxLayout(qr_box)
        qr_lay.setContentsMargins(8, 8, 8, 8)
        self._qr = QLabel(qr_box)
        self._qr.setAlignment(Qt.AlignCenter)
        self._qr.setFixedSize(168, 168)
        qr_lay.addWidget(self._qr, 0, Qt.AlignCenter)
        access_lay.addWidget(qr_box, 0, Qt.AlignCenter)

        # 校验码：手机扫码后还要在网页里输入这四位数字才能进入
        self._code_caption = QLabel(I18n.tr('mobile_access_code'), self._access)
        self._code_caption.setAlignment(Qt.AlignCenter)
        self._code_caption.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        access_lay.addWidget(self._code_caption)

        code_row = QHBoxLayout()
        code_row.setSpacing(8)
        code_row.addStretch()
        self._code_labels = []
        for _ in range(4):
            digit = QLabel('-', self._access)
            digit.setAlignment(Qt.AlignCenter)
            digit.setFixedSize(34, 40)
            digit.setStyleSheet(
                "background: palette(base); color: palette(text);"
                " border: 1px solid palette(mid); border-radius: 6px;"
                " font-size: 20px; font-weight: 600;"
            )
            code_row.addWidget(digit)
            self._code_labels.append(digit)
        code_row.addStretch()
        access_lay.addLayout(code_row)

        # 地址行：下拉即网卡选择（多网卡时切换），右侧复制链接
        addr_row = QHBoxLayout()
        addr_row.setSpacing(6)
        self._addr_pick = QComboBox(self._access)
        self._addr_pick.setFixedHeight(28)
        self._addr_pick.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._addr_pick.setStyleSheet(
            "QComboBox { background: palette(base); color: palette(text);"
            " border: 1px solid palette(mid); border-radius: 6px;"
            " padding: 0 8px; font-size: 11px; }"
            "QComboBox::drop-down { border: none; width: 18px; }"
        )
        self._addr_pick.currentIndexChanged.connect(self._on_addr_changed)
        addr_row.addWidget(self._addr_pick, 1)
        self._copy_btn = QPushButton(I18n.tr('mobile_access_copy'), self._access)
        self._copy_btn.setCursor(Qt.PointingHandCursor)
        self._copy_btn.setFixedHeight(28)
        self._copy_btn.setEnabled(False)
        self._copy_btn.setStyleSheet(
            "QPushButton { background: transparent; color: palette(text);"
            " border: 1px solid palette(mid); border-radius: 6px;"
            " font-size: 11px; padding: 0 10px; }"
            "QPushButton:hover { background: palette(midlight); }"
            "QPushButton:pressed { background: palette(mid); }"
            "QPushButton:disabled { color: palette(mid); }"
        )
        self._copy_btn.clicked.connect(self._copy_url)
        addr_row.addWidget(self._copy_btn, 0, Qt.AlignTop)
        access_lay.addLayout(addr_row)
        reveal.addWidget(self._access)

        # 说明行：未开启时提示如何扫码，开启后换成安全提醒。
        # 固定两行高度：两种文案行数不同，避免切换时版面跳动
        self._helper = QLabel('', self._reveal)
        self._helper.setWordWrap(True)
        self._helper.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._helper.setFixedHeight(30)
        self._helper.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        self._helper.setText(I18n.tr('mobile_access_hint'))
        reveal.addWidget(self._helper)

        lay.addWidget(self._reveal)

        # 展开区整体淡入淡出（与裁剪动画配合，避免露出被切断的文字）
        self._reveal_effect = QGraphicsOpacityEffect(self._reveal)
        self._reveal_effect.setOpacity(0.0)
        self._reveal.setGraphicsEffect(self._reveal_effect)

        self._show_placeholder()

    def _measure(self):
        """量出紧凑态（标题+开关）与展开态（含二维码区）两种高度。"""
        self._card.ensurePolished()
        lay = self._card.layout()
        lay.activate()
        m = lay.contentsMargins()
        self._compact_h = float(
            m.top() + self._title.sizeHint().height() + lay.spacing()
            + self._row.sizeHint().height() + m.bottom())
        self._full_h = (self._compact_h + lay.spacing()
                        + self._reveal.sizeHint().height())

    def _show_placeholder(self):
        """未开启态：二维码位置放占位说明，地址框禁用 —— 避免切换时抖动。"""
        self._qr.setPixmap(QPixmap())
        self._qr.setStyleSheet('color: #adb5bd; font-size: 11px;')
        self._qr.setText(I18n.tr('mobile_access_qr_placeholder'))
        self._addr_pick.blockSignals(True)
        self._addr_pick.clear()
        self._addr_pick.addItem(I18n.tr('mobile_access_off'))
        self._addr_pick.setEnabled(False)
        self._addr_pick.blockSignals(False)
        self._copy_btn.setEnabled(False)
        self._set_code('')

    def _is_dark(self) -> bool:
        win = self.palette().color(self.palette().ColorRole.Window)
        return (0.299 * win.red() + 0.587 * win.green()
                + 0.114 * win.blue()) < 128

    # ---------- 开关与生命周期 ----------

    def _on_switch(self, checked: bool):
        if checked:
            self._expand()
            self._start()
        else:
            self._stop()
            self._collapse()

    def _start(self):
        if self._starting or (self._service is not None and self._service.running):
            return
        self._want_running = True
        self._starting = True
        self._start_seq += 1
        self._switch.setEnabled(False)
        self._status.setText(I18n.tr('mobile_access_starting'))
        self._status.setStyleSheet('font-size: 11px; color: #339af0;')
        # 首次开启要初始化 uvicorn/FastAPI（数百毫秒），放到后台线程避免卡住界面
        threading.Thread(target=self._start_worker, args=(self._start_seq,),
                         daemon=True).start()

    def _start_worker(self, seq: int):
        """后台创建并启动服务；结果通过信号回到主线程。"""
        try:
            from web.service import MobileWebService
            owner = self._owner
            chat = getattr(owner, 'chat_panel', None)
            service = MobileWebService(
                owner.room_folder, owner.room_code, owner.is_host,
                bridge=owner.web_bridge,
                on_added=owner.web_after_file_added,
                on_deleted=owner.web_after_file_deleted,
                on_renamed=owner.web_after_file_renamed,
                on_dir_created=owner.web_after_dir_created,
                chat_peers=(lambda: chat.web_peers()) if chat else None,
                chat_history=(lambda peer: chat.web_history(peer)) if chat else None,
                chat_send=(lambda peer, text: chat.web_send_text(peer, text))
                if chat else None,
                chat_read=(lambda peer: chat.web_mark_read(peer)) if chat else None,
                room_state=owner.web_room_state,
                readonly=owner.is_web_readonly,
                version=Config.APP_VERSION_SERIAL,
                token=self._access_token,
            )
            info = service.start()
        except Exception as exc:
            self.start_finished.emit(seq, None, str(exc))
            return
        self.start_finished.emit(seq, service, info)

    def _on_start_finished(self, seq: int, service, payload):
        self._starting = False
        self._switch.setEnabled(True)
        if seq != self._start_seq:
            # 过期结果（期间被关闭或重新开启过）：丢弃并清理已启动的服务
            if service is not None:
                try:
                    service.stop()
                except Exception:
                    pass
            return
        if service is None:
            # 启动失败：复位开关并在状态行给出原因
            self._want_running = False
            self._set_error(str(payload))
            self._switch.blockSignals(True)
            self._switch.setChecked(False, animate=False)
            self._switch.blockSignals(False)
            self.running_changed.emit(False)
            return
        if not self._want_running:
            # 等待期间用户已关闭开关/退出房间：直接销毁刚启动的服务
            try:
                service.stop()
            except Exception:
                pass
            return
        self._service = service
        self._url_info = payload
        self._fill_addresses(payload)
        self._set_code(str(payload.get('code') or ''))
        self._helper.setText(I18n.tr('mobile_access_footer'))
        self._status.setStyleSheet('font-size: 11px; color: #339af0;')
        self._refresh_status()
        self._poll.start()
        self.running_changed.emit(True)

    def _stop(self):
        self._want_running = False
        self._starting = False
        self._start_seq += 1
        self._switch.setEnabled(True)
        self._poll.stop()
        if self._service is not None:
            try:
                self._service.stop()
            except Exception:
                pass
        self._service = None
        self._url_info = None
        self._show_placeholder()
        self._helper.setText(I18n.tr('mobile_access_hint'))
        muted = '#a0a0a0' if self._is_dark() else '#868e96'
        self._status.setText(I18n.tr('mobile_access_off'))
        self._status.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        self.running_changed.emit(False)

    def shutdown(self):
        """窗口关闭/退出房间：停服务并收起浮层。"""
        self._stop()
        self._set_expanded(False)
        self.hide()

    def _set_error(self, text: str):
        self._status.setText(text)
        self._status.setStyleSheet('font-size: 11px; color: #fa5252;')

    def _fill_addresses(self, info: dict):
        urls = info.get('urls') or []
        primary = info.get('primary_url', '')
        self._addr_pick.blockSignals(True)
        self._addr_pick.clear()
        for url in urls:
            self._addr_pick.addItem(url.split('/#t=')[0] + '/', url)
        self._addr_pick.setEnabled(True)
        self._copy_btn.setEnabled(True)
        idx = 0
        for i, url in enumerate(urls):
            if url == primary:
                idx = i
                break
        if urls:
            self._addr_pick.setCurrentIndex(idx)
        self._addr_pick.blockSignals(False)
        if urls:
            self._apply_url(urls[idx])

    def _on_addr_changed(self, index: int):
        url = self._addr_pick.itemData(index)
        if url:
            self._apply_url(url)

    def _set_code(self, code: str):
        """把四位校验码显示到方格里；空串显示占位横线。"""
        chars = (code or '').ljust(4, '-')[:4]
        for label, ch in zip(self._code_labels, chars):
            label.setText(ch)

    def _apply_url(self, url: str):
        if not url:
            return
        self._current_url = url
        self._qr.setStyleSheet('')
        self._qr.setText('')
        self._qr.setPixmap(_qr_pixmap(url, 168))

    def _copy_url(self):
        url = getattr(self, '_current_url', '')
        if not url:
            return
        QApplication.clipboard().setText(url)
        self._copy_btn.setText(I18n.tr('mobile_access_copied'))
        QTimer.singleShot(1200, lambda: self._copy_btn.setText(
            I18n.tr('mobile_access_copy')))

    def _refresh_status(self):
        if self._service is None:
            return
        count = self._service.session_count
        if count <= 0:
            self._status.setText(I18n.tr('mobile_access_waiting'))
            color = '#339af0'
        else:
            self._status.setText(
                I18n.tr('mobile_access_connected', count=str(count)))
            color = '#40c057'
        self._status.setStyleSheet(f'font-size: 11px; color: {color};')

    # ---------- 展开 / 收起 ----------

    def _body_h(self) -> float:
        return self._card.visible_h()

    def _set_body(self, h: float):
        """设置底板可见高度：只裁剪绘制与子控件，布局始终保持完整高度。"""
        self._card.set_visible_h(h)
        self._card.setMask(QRegion(0, 0, self._FULL_W,
                                   max(1, int(round(h)))))

    def _resize_window(self, h: float):
        """窗口高度跟随形态变化，只在切换时调用，不逐帧改变尺寸。"""
        h = max(1, int(round(h)))
        # 出现/收起动画进行中时同步终点高度，避免下一帧把尺寸改回去
        if self._anim is not None:
            end = self._anim.endValue()
            if end is not None:
                self._anim.setEndValue(QRect(end.x(), end.y(), self._FULL_W, h))
        self.resize(self._FULL_W, h)

    def _set_expanded(self, expanded: bool):
        """无动画地切到目标形态，用于浮层显示前恢复上次状态。"""
        self._expanded = expanded
        self._state_seq += 1
        self._stop_body_anim()
        self._stop_reveal_anim()
        target = self._full_h if expanded else self._compact_h
        self._reveal_effect.setOpacity(1.0 if expanded else 0.0)
        self._set_body(target)
        self._resize_window(target)

    def _expand(self):
        """先撑开面板，再让展开区内容淡入。"""
        if self._expanded:
            return
        self._expanded = True
        self._state_seq += 1
        seq = self._state_seq
        self._resize_window(self._full_h)
        self._animate_body(self._body_h(), self._full_h, self._EXPAND_MS)
        QTimer.singleShot(self._REVEAL_DELAY, lambda: self._reveal_in(seq))

    def _reveal_in(self, seq: int):
        if seq != self._state_seq or not self._expanded:
            return
        self._animate_reveal(1.0, self._REVEAL_MS)

    def _collapse(self):
        """内容淡出与面板收起同时进行，避免中途出现整块空白面板。"""
        if not self._expanded:
            return
        self._expanded = False
        self._state_seq += 1
        self._animate_body(self._body_h(), self._compact_h, self._EXPAND_MS)
        self._animate_reveal(0.0, 140)

    def _animate_body(self, from_h: float, to_h: float, duration: int):
        self._stop_body_anim()
        anim = QVariantAnimation(self)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(float(from_h))
        anim.setEndValue(float(to_h))
        anim.valueChanged.connect(lambda v: self._set_body(float(v)))
        anim.finished.connect(self._on_body_settled)
        self._body_anim = anim
        anim.start()

    def _on_body_settled(self):
        self._body_anim = None
        if self._expanded:
            self._resize_window(self._full_h)
        else:
            self._set_body(self._compact_h)
            self._resize_window(self._compact_h)

    def _animate_reveal(self, to_opacity: float, duration: int, on_end=None):
        self._stop_reveal_anim()
        anim = QPropertyAnimation(self._reveal_effect, b"opacity", self)
        anim.setDuration(duration)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.setStartValue(float(self._reveal_effect.opacity()))
        anim.setEndValue(float(to_opacity))
        if on_end is not None:
            anim.finished.connect(on_end)
        self._reveal_anim = anim
        anim.start()

    def _stop_body_anim(self):
        if self._body_anim is not None:
            self._body_anim.stop()
            self._body_anim.deleteLater()
            self._body_anim = None

    def _stop_reveal_anim(self):
        if self._reveal_anim is not None:
            self._reveal_anim.stop()
            self._reveal_anim.deleteLater()
            self._reveal_anim = None

    # ---------- 出现 / 收起 ----------

    def open_animated(self, right: int, top: int):
        """在指定全局坐标处显示（right/top 为窗口右上角）。

        窗口按当前形态（紧凑或展开）一次性摆好，只对窗口整体做 12px 位移 +
        透明度渐入；本浮层挂在窗口顶部，自上向下落位。
        """
        self._anchor_right = right
        self._anchor_top = top
        self._closing = False
        self._set_expanded(self._expanded)
        h = int(round(self._body_h()))
        end = QRect(right - self._FULL_W, top, self._FULL_W, h)
        start = end.translated(0, -self._RISE)
        self.setGeometry(start)
        self.setWindowOpacity(0.0)
        self.show()
        self._install_outside_filter()
        self._animate(start, end, 0.0, 1.0)

    def open_at_widget(self, btn):
        """锚定到按钮下方并向下展开；下方空间不足时上移夹在屏幕内。"""
        pos = btn.mapToGlobal(btn.rect().bottomRight())
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        right = pos.x()
        top = pos.y() + 4
        if screen is not None:
            geo = screen.availableGeometry()
            right = max(geo.left() + self._FULL_W + 8, min(right, geo.right() - 8))
            top = max(geo.top() + 8,
                      min(top, geo.bottom() - 8 - int(self._full_h)))
        self.open_animated(right, top)

    def close_animated(self):
        """淡出并上移 12px 后隐藏（与出现方向相反的同一套收起动效）。"""
        if not self.isVisible() or self._closing:
            return
        self._closing = True
        end = QRect(self._anchor_right - self._FULL_W, self._anchor_top,
                    self._FULL_W, self.height())
        start = end.translated(0, -self._RISE)
        self._animate(self.geometry(), start, self.windowOpacity(), 0.0,
                      on_end=self._after_collapse)

    def _animate(self, rect_from, rect_to, opacity_from, opacity_to,
                 on_end=None):
        self._stop_anim()
        geo = QPropertyAnimation(self, b"geometry", self)
        geo.setDuration(self._ANIM_MS)
        geo.setEasingCurve(QEasingCurve.OutCubic)
        geo.setStartValue(rect_from)
        geo.setEndValue(rect_to)
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(self._ANIM_MS)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        fade.setStartValue(float(opacity_from))
        fade.setEndValue(float(opacity_to))
        fade.finished.connect(self._on_anim_finished)
        if on_end is not None:
            fade.finished.connect(on_end)
        self._anim = geo
        self._fade = fade
        geo.start()
        fade.start()

    def _on_anim_finished(self):
        self._anim = None
        self._fade = None

    def _stop_anim(self):
        for anim in (self._anim, self._fade):
            if anim is not None:
                anim.stop()
                anim.deleteLater()
        self._anim = None
        self._fade = None

    def _after_collapse(self):
        self._closing = False
        self._remove_outside_filter()
        self.hide()

    # ---------- 点外/Esc 关闭 ----------

    def _install_outside_filter(self):
        if self._outside_filter:
            return
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
            self._outside_filter = True

    def _remove_outside_filter(self):
        if not self._outside_filter:
            return
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        self._outside_filter = False

    def eventFilter(self, obj, event):
        if self.isVisible() and not self._closing:
            if event.type() == QEvent.MouseButtonPress:
                if isinstance(obj, QWidget):
                    pos = event.globalPosition().toPoint()
                    # 只认卡片当前可见区域：展开动画期间多出的透明区域点击视为外部
                    origin = self._card.mapToGlobal(QPoint(0, 0))
                    body = QRect(origin, QSize(self._FULL_W,
                                               max(1, int(round(self._body_h())))))
                    if not body.contains(pos):
                        self.close_animated()
            elif event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
                self.close_animated()
                return True
            elif event.type() == QEvent.ApplicationDeactivate:
                self.close_animated()
        return super().eventFilter(obj, event)

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate and self.isVisible() and not self._closing:
            self.close_animated()
        return super().event(e)

    def hideEvent(self, event):
        self._hidden_ts = time.monotonic()
        self._stop_anim()
        self._stop_body_anim()
        self._stop_reveal_anim()
        self._state_seq += 1
        self._closing = False
        self._remove_outside_filter()
        super().hideEvent(event)

    def just_hidden(self) -> bool:
        return time.monotonic() - getattr(self, '_hidden_ts', 0.0) < 0.25
