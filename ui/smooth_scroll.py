"""
平滑滚动列表控件
将滚轮的离散事件转换为带缓动的连续滚动动画，获得丝滑手感。
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
from PySide6.QtCore import QPropertyAnimation, QEasingCurve, QAbstractAnimation
from PySide6.QtWidgets import QListWidget, QAbstractItemView


class SmoothScrollList(QListWidget):
    """带平滑滚动动画的列表控件

    滚轮事件不直接跳变，而是把期望滚动位置累加后经 OutCubic 缓动动画平滑过渡；
    连续滚动时基于目标值累加，滚得越多越有惯性缓冲的丝滑感。
    """

    WHEEL_STEP = 20       # 滚轮单格像素步长
    WHEEL_LINES = 3       # 每格换算的行数（放大滚轮灵敏）
    MIN_DURATION = 200    # 最短动画时长(ms)
    DURATION_PER_PX = 1   # 每像素增加的时长系数

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.verticalScrollBar().setSingleStep(self.WHEEL_STEP)
        self._target = 0.0
        self._anim = None

    def _scroll_to(self, target: float):
        """将滚动条平滑动画到目标位置"""
        sb = self.verticalScrollBar()
        target = max(float(sb.minimum()), min(float(sb.maximum()), float(target)))

        # 目标与当前几乎一致：停止动画即可
        if abs(sb.value() - target) < 0.5:
            self._target = target
            if self._anim:
                self._anim.stop()
            return

        # 重新定位起点为当前实际值，向新目标平滑过渡
        if self._anim:
            self._anim.stop()
        self._target = target

        distance = abs(sb.value() - target)
        duration = int(self.MIN_DURATION + distance * self.DURATION_PER_PX)
        anim = QPropertyAnimation(sb, b"value", self)
        anim.setDuration(duration)
        anim.setStartValue(float(sb.value()))
        anim.setEndValue(self._target)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start()
        self._anim = anim

    def wheelEvent(self, event):
        """拦截滚轮：累加目标后平滑滚动，形成惯性手感"""
        sb = self.verticalScrollBar()
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return

        step = float(sb.singleStep()) * self.WHEEL_LINES
        # 动画运行时从目标累加（保证连续滚动连贯、累积出惯性），否则从当前值起步
        running = self._anim and self._anim.state() == QAbstractAnimation.Running
        base = self._target if running else float(sb.value())
        new_target = base - delta / 120.0 * step
        self._scroll_to(new_target)
        event.accept()