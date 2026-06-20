"""
全局UI组件
"""
from PySide6.QtWidgets import QPushButton, QLabel, QFrame, QGraphicsOpacityEffect
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QByteArray, QPoint, QEvent, Signal
from PySide6.QtGui import QFont
from functools import partial


class AnimatedButton(QPushButton):
    """
    带动画效果的按钮类
    - 点击时有按下效果（向下移动1px模拟下沉）
    - 释放时恢复原始位置
    """

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self._original_pos = None      # 保存原始位置
        self._is_pressed = False       # 标记是否处于按下状态

    def event(self, event):
        """处理所有事件，包括布局改变事件"""
        # 当布局改变时（窗口大小调整），清除保存的位置
        if event.type() == QEvent.LayoutRequest:
            if not self._is_pressed:
                self._original_pos = None
        return super().event(event)

    def mousePressEvent(self, event):
        """鼠标按下 - 向下移动1px模拟按下效果"""
        if event.button() != Qt.LeftButton:
            super().mousePressEvent(event)
            return
            
        # 每次点击时重新获取当前位置
        self._original_pos = self.pos()

        self._is_pressed = True
        super().mousePressEvent(event)
        
        # 在事件处理后移动
        if self._original_pos:
            self.move(QPoint(self._original_pos.x(), self._original_pos.y() + 1))

    def mouseReleaseEvent(self, event):
        """鼠标释放 - 恢复原始位置"""
        if event.button() != Qt.LeftButton:
            super().mouseReleaseEvent(event)
            return
            
        self._is_pressed = False
        if self._original_pos is not None:
            self.move(self._original_pos)
        super().mouseReleaseEvent(event)


class NotificationBanner(QFrame):
    """
    顶部浮动通知横幅 - 置顶重叠显示，不挤压上方元素

    支持类型:
        success (绿色) / error (红色) / warning (黄色) / info (蓝色)
    """

    SUCCESS = 'success'
    ERROR = 'error'
    WARNING = 'warning'
    INFO = 'info'

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # 定时器：自动隐藏
        self._timeout_timer = QTimer(self)
        self._timeout_timer.setSingleShot(True)
        self._timeout_timer.timeout.connect(self._start_hide_animation)

        # 动画相关
        self._fade_animation = None
        self._opacity_effect = None
        self._is_animating = False
        self._is_showing = False  # 防止重复显示

        self.setVisible(False)
        self._init_ui()

    def _init_ui(self):
        """初始化界面"""
        self.setFrameShape(QFrame.StyledPanel)
        
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)
        
        # 消息标签
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label, 1)
        
        # 关闭按钮
        self.close_btn = QPushButton("×")
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setFlat(True)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.clicked.connect(self.dismiss)
        layout.addWidget(self.close_btn)

    def show_message(self, message, type='success', duration=3500):
        """
        显示通知横幅

        Args:
            message: 通知文本
            type: success / error / warning / info
            duration: 自动隐藏毫秒（0 表示不自动隐藏）
        """
        # 防止重复显示
        if self._is_showing:
            return
        self._is_showing = True
        
        # 停止旧的动画
        self._stop_animations()

        # 设置样式和内容
        self.message_label.setText(message)
        
        # 根据类型设置样式
        styles = {
            self.SUCCESS: {
                'bg': '#d3f9d8',
                'border': '#2b8a3e',
                'text': '#2b8a3e'
            },
            self.ERROR: {
                'bg': '#ffe3e3',
                'border': '#c92a2a',
                'text': '#c92a2a'
            },
            self.WARNING: {
                'bg': '#fff3bf',
                'border': '#e67700',
                'text': '#e67700'
            },
            self.INFO: {
                'bg': '#d0ebff',
                'border': '#1971c2',
                'text': '#1971c2'
            }
        }
        
        style = styles.get(type, styles[self.SUCCESS])
        self.setStyleSheet(f"""
            QFrame {{
                background-color: {style['bg']};
                border: 1px solid {style['border']};
                border-radius: 6px;
            }}
            QLabel {{
                color: {style['text']};
                font-size: 13px;
            }}
            QPushButton {{
                color: {style['text']};
                font-size: 16px;
                font-weight: bold;
                border: none;
            }}
            QPushButton:hover {{
                background-color: rgba(0, 0, 0, 0.1);
                border-radius: 10px;
            }}
        """)

        # 淡入动画
        self._start_show_animation()

        # 定时自动隐藏
        if duration > 0:
            self._timeout_timer.start(duration)

    def _start_show_animation(self):
        """淡入显示"""
        self._is_animating = True

        # 创建透明度效果
        if not self._opacity_effect:
            self._opacity_effect = QGraphicsOpacityEffect(self)
            self.setGraphicsEffect(self._opacity_effect)

        self._opacity_effect.setOpacity(0.0)
        self.setVisible(True)

        # 创建动画
        anim = QPropertyAnimation(self._opacity_effect, QByteArray(b"opacity"))
        anim.setDuration(200)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        self._fade_animation = anim
        anim.finished.connect(self._on_show_finished)
        anim.start()

    def _start_hide_animation(self):
        """淡出隐藏"""
        if self._is_animating or not self.isVisible():
            return

        self._is_animating = True
        self._timeout_timer.stop()

        # 创建动画
        anim = QPropertyAnimation(self._opacity_effect, QByteArray(b"opacity"))
        anim.setDuration(250)
        anim.setStartValue(self._opacity_effect.opacity())
        anim.setEndValue(0.0)
        self._fade_animation = anim
        anim.finished.connect(self._on_hide_finished)
        anim.start()

    def _on_show_finished(self):
        """淡入完成"""
        self._is_animating = False
        self._fade_animation = None

    def _on_hide_finished(self):
        """淡出完成"""
        self._is_animating = False
        self._fade_animation = None
        self._is_showing = False
        self.setVisible(False)

    def _stop_animations(self):
        """停止所有动画"""
        if self._fade_animation:
            try:
                self._fade_animation.finished.disconnect()
            except:
                pass
            self._fade_animation.stop()
            self._fade_animation.deleteLater()
            self._fade_animation = None
        self._timeout_timer.stop()
        self._is_animating = False
        self._is_showing = False

    def dismiss(self):
        """手动关闭"""
        self._start_hide_animation()


def fade_widget(parent, widget, visible, duration=150):
    """
    控件淡入淡出动画
    
    Args:
        parent: 父对象（需要有 _fade_animations 字典）
        widget: 要动画的控件
        visible: True 显示（淡入），False 隐藏（淡出）
        duration: 动画持续时间（毫秒）
    """
    if not hasattr(parent, '_fade_animations'):
        parent._fade_animations = {}  # 字典存储 {widget: animation}

    # 停止并清理该widget的旧动画
    old_animation = parent._fade_animations.get(widget)
    if old_animation:
        try:
            # 先停止动画
            old_animation.stop()
            # 从字典中移除
            parent._fade_animations.pop(widget, None)
            # 延迟删除
            old_animation.deleteLater()
        except RuntimeError:
            # 对象已被删除
            parent._fade_animations.pop(widget, None)

    # 获取或创建透明度效果
    effect = widget.graphicsEffect()
    if not effect or not isinstance(effect, QGraphicsOpacityEffect):
        effect = QGraphicsOpacityEffect(widget)
        widget.setGraphicsEffect(effect)

    # 创建动画
    animation = QPropertyAnimation(effect, QByteArray(b"opacity"))
    animation.setDuration(duration)
    parent._fade_animations[widget] = animation

    if visible:
        # 淡入
        widget.setVisible(True)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
    else:
        # 淡出
        animation.setStartValue(1.0)
        animation.setEndValue(0.0)
        animation.finished.connect(partial(_on_fade_finished, parent, widget))

    animation.start()


def _on_fade_finished(parent, widget):
    """淡出动画完成回调"""
    widget.setVisible(False)
    if hasattr(parent, '_fade_animations'):
        parent._fade_animations.pop(widget, None)


# 按钮样式常量
BUTTON_STYLES = {
    'primary': """
        QPushButton {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 6px;
            background-color: #339af0;
            color: white;
            border: none;
        }
        QPushButton:hover {
            background-color: #228be6;
        }
        QPushButton:pressed {
            background-color: #1c7ed6;
        }
        QPushButton:disabled {
            background-color: #adb5bd;
        }
    """,
    'success': """
        QPushButton {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 6px;
            background-color: #51cf66;
            color: white;
            border: none;
        }
        QPushButton:hover {
            background-color: #40c057;
        }
        QPushButton:pressed {
            background-color: #37b24d;
        }
        QPushButton:disabled {
            background-color: #adb5bd;
        }
    """,
    'danger': """
        QPushButton {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 6px;
            background-color: #ff6b6b;
            color: white;
            border: none;
        }
        QPushButton:hover {
            background-color: #fa5252;
        }
        QPushButton:pressed {
            background-color: #f03e3e;
        }
        QPushButton:disabled {
            background-color: #adb5bd;
        }
    """,
    'secondary': """
        QPushButton {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 6px;
            background-color: #868e96;
            color: white;
            border: none;
        }
        QPushButton:hover {
            background-color: #495057;
        }
        QPushButton:pressed {
            background-color: #343a40;
        }
        QPushButton:disabled {
            background-color: #adb5bd;
        }
    """,
    'outline': """
        QPushButton {
            padding: 8px 16px;
            font-size: 13px;
            border-radius: 6px;
            background-color: transparent;
            color: #339af0;
            border: 1px solid #339af0;
        }
        QPushButton:hover {
            background-color: #e7f5ff;
        }
        QPushButton:pressed {
            background-color: #d0ebff;
        }
        QPushButton:disabled {
            color: #adb5bd;
            border-color: #adb5bd;
        }
    """
}
