"""
手机访问浮层
开关局域网网页服务、展示二维码与访问地址
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import threading
import time

from PySide6.QtCore import (Qt, QEvent, QTimer, QRect, QPropertyAnimation,
                            QEasingCurve, QPoint, Signal)
from PySide6.QtGui import QPixmap, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QFrame, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QComboBox, QWidget, QSizePolicy,
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


class MobileAccessCard(QFrame):
    """「手机访问」浮动卡片。"""

    # 后台启动结果：(序号, service, info)；失败时 service 为 None
    start_finished = Signal(int, object, object)

    _ANIM_MS = 180            # 出现/收起时长（与投递设置浮层一致）
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
        self._outside_filter = False

        self._card = QFrame(self)
        self._card.setObjectName('mobileAccessCard')
        self._card.setStyleSheet(
            "QFrame#mobileAccessCard { background: palette(window);"
            " border: 1px solid palette(mid); border-radius: 10px; }"
        )

        self._build_ui()

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
        title = QLabel(I18n.tr('mobile_access'))
        title.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        lay.addWidget(title)

        # 开关行：复用投递设置面板的开关行排版（0/4 内边距 + 12 间距）
        row_widget = QWidget(self._card)
        row = QHBoxLayout(row_widget)
        row.setContentsMargins(0, 4, 0, 4)
        row.setSpacing(12)
        switch_label = QLabel(I18n.tr('mobile_access_switch'))
        row.addWidget(switch_label)
        row.addStretch()
        self._switch = ToggleSwitch()
        self._switch.stateChanged.connect(self._on_switch)
        row.addWidget(self._switch)
        lay.addWidget(row_widget)

        # 状态行：常驻（未开启显示灰字），保证开关切换时卡片高度不变
        self._status = QLabel(I18n.tr('mobile_access_off'), self._card)
        self._status.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        lay.addWidget(self._status)

        # 内容区常驻：未开启时用占位图与禁用控件填充，切换开关不改变高度
        self._access = QWidget(self._card)
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

        lay.addWidget(self._access)
        self._show_placeholder()

        # 说明行：未开启时提示如何扫码，开启后换成安全提醒。
        # 固定预留两行高度：两种文案行数不同，若让它随文本伸缩会把上方内容顶动
        self._helper = QLabel('', self._card)
        self._helper.setWordWrap(True)
        self._helper.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        self._helper.setFixedHeight(30)
        self._helper.setStyleSheet('font-size: 11px; color: ' + muted + ';')
        self._helper.setText(I18n.tr('mobile_access_hint'))
        lay.addWidget(self._helper)

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

    def _is_dark(self) -> bool:
        win = self.palette().color(self.palette().ColorRole.Window)
        return (0.299 * win.red() + 0.587 * win.green()
                + 0.114 * win.blue()) < 128

    # ---------- 开关与生命周期 ----------

    def _on_switch(self, checked: bool):
        if checked:
            self._start()
        else:
            self._stop()

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
        self._helper.setText(I18n.tr('mobile_access_footer'))
        self._status.setStyleSheet('font-size: 11px; color: #339af0;')
        self._refresh_status()
        self._poll.start()

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

    def shutdown(self):
        """窗口关闭/退出房间：停服务并收起浮层。"""
        self._stop()
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

    # ---------- 布局与动效 ----------

    def _full_height(self) -> int:
        lay = self._card.layout()
        if lay is not None:
            lay.activate()
        return max(1, self._card.sizeHint().height())

    def open_animated(self, right: int, top: int):
        """在指定全局坐标处展开（right/top 为卡片右上角）。

        动效与「投递设置」浮层同源：窗口一次性按完整尺寸摆好，只对窗口整体
        做 12px 位移 + 透明度渐入，不裁切、不逐帧缩放，内容不重排。
        本浮层挂在窗口顶部，方向与底部胶囊相反：自上向下落位。
        """
        self._anchor_right = right
        self._anchor_top = top
        self._closing = False
        h = self._full_height()
        self._card.setGeometry(0, 0, self._FULL_W, h)
        end = QRect(right - self._FULL_W, top, self._FULL_W, h)
        start = end.translated(0, -self._RISE)
        self.setGeometry(start)
        self.setWindowOpacity(0.0)
        self.show()
        self._install_outside_filter()
        self._animate(start, end, 0.0, 1.0)

    def open_at_widget(self, btn):
        """锚定到某个按钮：默认贴其右下方，空间不足时改到上方。"""
        h = self._full_height()
        pos = btn.mapToGlobal(btn.rect().bottomRight())
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        right = pos.x()
        top = pos.y() + 4
        if screen is not None:
            geo = screen.availableGeometry()
            right = max(geo.left() + self._FULL_W + 8, min(right, geo.right() - 8))
            if top + h > geo.bottom() - 8:
                top = max(geo.top() + 8, pos.y() - btn.height() - 8 - h)
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
                    # 只认卡片实体区域：动画期间窗口多出的透明区域点击视为外部
                    origin = self._card.mapToGlobal(QPoint(0, 0))
                    if not QRect(origin, self._card.size()).contains(pos):
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
        self._closing = False
        self._remove_outside_filter()
        super().hideEvent(event)

    def just_hidden(self) -> bool:
        return time.monotonic() - getattr(self, '_hidden_ts', 0.0) < 0.25
