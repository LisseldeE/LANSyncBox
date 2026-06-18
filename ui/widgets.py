# -*- coding: utf-8 -*-
"""
LANSyncBox 自定义UI控件
"""

from PyQt5.QtWidgets import QPushButton, QLabel, QWidget, QGraphicsOpacityEffect
from PyQt5.QtCore import QPoint, QPropertyAnimation, QByteArray, Qt


class AnimatedButton(QPushButton):
    """
    带动画效果的按钮类
    - 点击时有按下效果（向下移动1px）
    """
    
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.original_pos = None
        self.is_pressed = False
    
    def mousePressEvent(self, event):
        """鼠标按下事件 - 触发点击动画"""
        if self.original_pos is None:
            self.original_pos = self.pos()
        
        self.is_pressed = True
        # 向下移动1px，模拟按下效果
        self.move(QPoint(self.original_pos.x(), self.original_pos.y() + 1))
        super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event):
        """鼠标释放事件 - 恢复位置"""
        self.is_pressed = False
        if self.original_pos is not None:
            self.move(self.original_pos)
        super().mouseReleaseEvent(event)


def fade_widget(widget: QWidget, visible: bool, duration: int = 150, callback=None):
    """
    控件淡入淡出动画
    Args:
        widget: 要动画的控件
        visible: True 显示（淡入），False 隐藏（淡出）
        duration: 动画持续时间（毫秒）
        callback: 动画完成后的回调函数
    """
    # 创建透明度效果
    effect = QGraphicsOpacityEffect(widget)
    widget.setGraphicsEffect(effect)
    
    # 创建动画
    animation = QPropertyAnimation(effect, QByteArray(b"opacity"))
    animation.setDuration(duration)
    
    if visible:
        # 淡入：从0到1
        widget.setVisible(True)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
    else:
        # 淡出：从1到0
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        # 动画完成后隐藏控件
        animation.finished.connect(lambda: widget.setVisible(False))
    
    # 如果有回调函数，动画完成后执行
    if callback:
        animation.finished.connect(callback)
    
    animation.start()
    
    # 返回动画对象，防止被垃圾回收
    return animation


class ClickableLabel(QLabel):
    """可点击的标签"""
    
    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self._click_callback = None
    
    def setClickCallback(self, callback):
        """设置点击回调"""
        self._click_callback = callback
    
    def mousePressEvent(self, event):
        """鼠标点击事件"""
        if self._click_callback:
            self._click_callback()
        super().mousePressEvent(event)