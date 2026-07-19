"""
平行四边形扫描加载动画 - PySide6 实现
版权所有：Lisselde_E（GitHub:https://github.com/LisseldeE）

支持两种状态：
- 状态1（普通）：单向扫描 + 底部脉冲拖尾
- 状态2（中间状态）：左右循环扫描，无拖尾，用于"加载中/思考中/运行中"
"""

from PySide6.QtWidgets import QWidget, QVBoxLayout
from PySide6.QtCore import QTimer, Qt, QPropertyAnimation, Property, QEasingCurve, QRectF
from PySide6.QtGui import QPainter, QColor, QLinearGradient, QPainterPath, QPalette
from enum import Enum
import math


class LoaderState(Enum):
    """加载器状态枚举"""
    NORMAL = 1      # 运行中状态：单向扫描 + 拖尾
    INTERMEDIATE = 2  # 中间状态：左右循环扫描，无拖尾


class StatusColor(Enum):
    """状态颜色枚举（用于红/黄/绿状态指示）"""
    NONE = 0       # 不显示状态指示
    RED = 1        # 红色：错误/停止/失败
    YELLOW = 2     # 黄色：警告/等待/暂停
    GREEN = 3      # 绿色：成功/完成/正常


class ScannerWidget(QWidget):
    """平行四边形扫描器"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 增加宽度以容纳skewX后的扩展，向上取整确保完整显示
        skew_offset = 8 * 0.36
        self.setFixedSize(int(80 + skew_offset) + 1, 8)

        # 扫描光束位置 (0-100)
        self._scan_position = 0

        # 动画模式：True = 循环来回，False = 单向
        self._is_intermediate_mode = False

        # 状态颜色（红/黄/绿）
        self._status_color = StatusColor.NONE

        # 状态颜色映射
        self._status_colors = {
            StatusColor.RED: QColor("#ef4444"),      # 红色
            StatusColor.YELLOW: QColor("#f59e0b"),   # 黄色
            StatusColor.GREEN: QColor("#22c55e"),    # 绿色
        }

        # 状态呼吸动画值 (0-1)
        self._status_breath = 0.0

    def get_background_color(self):
        """根据当前主题获取背景色"""
        # 获取窗口背景色判断深色/浅色模式
        palette = self.palette()
        bg_color = palette.color(QPalette.Window)

        # 计算亮度
        luminance = (bg_color.red() * 0.299 + bg_color.green() * 0.587 + bg_color.blue() * 0.114)

        # 深色模式：使用稍深于界面的颜色
        if luminance < 128:
            # 使用稍深的颜色，但不要深到纯黑
            darker_color = bg_color.darker(120)  # 比背景色深20%，避免过黑
            return darker_color
        # 浅色模式：使用更明显的浅灰色
        else:
            # 使用稍深一点的灰色，确保在浅色背景下可见
            return QColor("#e0e0e0")

    def set_status_color(self, status: StatusColor):
        """设置状态颜色"""
        self._status_color = status
        self._status_breath = 0.0  # 重置呼吸动画
        self.update()

    def get_status_color(self) -> StatusColor:
        """获取当前状态颜色"""
        return self._status_color

    def set_status_breath(self, value: float):
        """设置状态呼吸动画值 (0-1)"""
        self._status_breath = value
        self.update()

    def set_intermediate_mode(self, enabled: bool):
        """设置是否为中间状态模式（左右循环扫描）"""
        self._is_intermediate_mode = enabled
        self.update()

    def get_scan_position(self):
        return self._scan_position

    def set_scan_position(self, value):
        self._scan_position = value
        self.update()

    scan_position = Property(float, get_scan_position, set_scan_position)

    def set_animation_progress(self, progress):
        """设置动画进度 (0-1)，与拖尾同步"""
        if self._is_intermediate_mode:
            # 中间状态：左右循环来回扫描
            # 使用三角函数实现平滑的来回运动
            # progress 从 0 到 1 对应一个完整的来回周期
            # 使用 sin 函数，让光束在 0-100 之间来回移动
            # sin 的范围是 -1 到 1，映射到 0-100
            self._scan_position = (math.sin(progress * math.pi * 2) + 1) * 50
        else:
            # 运行中状态：从左到右单向扫描
            # progress 从 0 到 1，对应扫描位置从 -30 到 130
            self._scan_position = -30 + progress * 160
        self.update()

    def paintEvent(self, event):
        """绘制平行四边形扫描器"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        total_width = self.width()
        height = self.height()
        scan_width = 80  # CSS中定义的扫描器宽度

        # 创建平行四边形路径 (skewX(-20deg))
        skew_offset = height * 0.36  # |tan(-20°)| ≈ 0.36

        # 精确对应CSS的skewX(-20deg)效果
        path = QPainterPath()
        path.moveTo(skew_offset, 0)  # 左上角
        path.lineTo(scan_width + skew_offset, 0)  # 右上角
        path.lineTo(scan_width, height)  # 右下角
        path.lineTo(0, height)  # 左下角
        path.closeSubpath()

        # 绘制背景色（根据主题动态调整）
        bg_color = self.get_background_color()
        painter.fillPath(path, bg_color)

        # 设置裁剪路径（确保填充不超出平行四边形）
        painter.setClipPath(path)

        # 绘制状态颜色填充（如果有设置状态颜色）
        if self._status_color != StatusColor.NONE:
            status_color = self._status_colors.get(self._status_color)
            if status_color:
                # 计算呼吸动画的透明度系数 (0.85-1.0)
                breath_factor = 0.85 + 0.15 * math.sin(self._status_breath * math.pi * 2)

                # 状态指示模式：均匀填充，带呼吸动画，无渐变
                alpha = int(200 * breath_factor)
                fill_color = QColor(status_color.red(), status_color.green(), status_color.blue(), alpha)

                # 填充整个平行四边形区域（x从0开始，宽度为scan_width）
                painter.fillRect(QRectF(0, 0, scan_width + skew_offset, height), fill_color)
        else:
            # 普通模式：绘制扫描光束
            beam_width = scan_width * 0.3
            beam_x = (self._scan_position / 100.0) * scan_width - beam_width / 2 + skew_offset

            # 创建渐变
            gradient = QLinearGradient(beam_x, 0, beam_x + beam_width, 0)
            gradient.setColorAt(0.0, QColor(59, 130, 246, 0))
            gradient.setColorAt(0.5, QColor(59, 130, 246, 200))
            gradient.setColorAt(1.0, QColor(59, 130, 246, 0))

            painter.fillRect(QRectF(beam_x, 0, beam_width, height), gradient)

        painter.setClipping(False)


class TrailWidget(QWidget):
    """拖尾线条容器"""

    def __init__(self, parent=None):
        super().__init__(parent)
        # 增加宽度以容纳skewX后的扩展：4个小块 + 3个间距 + 最后一个的偏移
        skew_offset = int(3 * 0.36)
        self.setFixedSize(64 + skew_offset, 3)

        # 四个拖尾的脉冲延迟（紧凑的波浪效果）
        self.trail_delays = [0.0, 0.10, 0.20, 0.30]

        # 动画时间 (0-1周期)
        self.animation_time = 0.0

        # 颜色
        self.accent_blue = QColor("#3b82f6")
        self.border_light = QColor("#2a2a36")

    def set_animation_progress(self, progress):
        """设置动画进度 (0-1)，与扫描器同步"""
        self.animation_time = progress
        self.update()

    def paintEvent(self, event):
        """绘制四个拖尾小条"""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        height = self.height()
        trail_width = 12
        gap = 4

        # skewX(-20deg) 的偏移
        skew_offset = height * 0.36

        for i in range(4):
            # 计算脉冲值：每个拖尾依次达到峰值
            # 脉冲周期为0.4（总周期1.2），依次延迟0.08
            phase = (self.animation_time - self.trail_delays[i]) % 1.0

            # 更快节奏的淡入淡出（缩短有效动画时间）
            if phase < 0.35:
                # 淡入：从 0 到 1（快速）
                pulse_value = self._ease_in_out(phase / 0.35)
            elif phase < 0.7:
                # 淡出：从 1 到 0（快速）
                pulse_value = self._ease_in_out(1.0 - (phase - 0.35) / 0.35)
            else:
                # 保持暗（等待下一轮）
                pulse_value = 0.0

            # 计算透明度和颜色（降低基础透明度，增加对比度）
            opacity = 0.05 + 0.95 * pulse_value

            if pulse_value > 0.3:
                color = QColor(59, 130, 246, int(255 * opacity))
            else:
                color = QColor(42, 42, 54, int(255 * opacity))

            # 计算位置（考虑skewX的偏移）
            x = i * (trail_width + gap)

            # 绘制倾斜的小平行四边形（skewX(-20deg)）
            # 上边保持不动，下边向左偏移
            path = QPainterPath()
            path.moveTo(x + skew_offset, 0)  # 左上角
            path.lineTo(x + trail_width + skew_offset, 0)  # 右上角
            path.lineTo(x + trail_width, height)  # 右下角
            path.lineTo(x, height)  # 左下角
            path.closeSubpath()

            painter.fillPath(path, color)

    def _ease_in_out(self, t):
        """ease-in-out 缓动函数"""
        if t < 0.5:
            return 2 * t * t
        else:
            return 1 - pow(-2 * t + 2, 2) / 2


class PageLoader(QWidget):
    """页面加载器"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.animation_progress = 0.0
        self._state = LoaderState.NORMAL  # 当前状态
        self.setup_ui()
        self.start_animation()

    def setup_ui(self):
        """设置UI"""
        # 设置窗口属性（不设置背景色）
        self.setAttribute(Qt.WA_StyledBackground, True)
        # 不设置背景色，保持透明
        # self.setStyleSheet("background-color: #0a0a0f;")

        # 主布局
        layout = QVBoxLayout(self)
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # 添加弹性空间
        layout.addStretch()

        # 扫描器容器（用于居中）
        scanner_container = QWidget()
        scanner_layout = QVBoxLayout(scanner_container)
        scanner_layout.setContentsMargins(0, 0, 0, 0)
        scanner_layout.setAlignment(Qt.AlignCenter)

        # 扫描器
        self.scanner = ScannerWidget()
        scanner_layout.addWidget(self.scanner, 0, Qt.AlignCenter)

        layout.addWidget(scanner_container)

        # 添加扫描器和拖尾之间的间距（原设计6px）
        self.spacing_widget = QWidget()
        self.spacing_widget.setFixedHeight(6)
        layout.addWidget(self.spacing_widget)

        # 拖尾线条容器
        self.trails_container = QWidget()
        trails_layout = QVBoxLayout(self.trails_container)
        trails_layout.setContentsMargins(0, 0, 0, 0)
        trails_layout.setAlignment(Qt.AlignCenter)

        # 拖尾线条
        self.trails = TrailWidget()
        trails_layout.addWidget(self.trails, 0, Qt.AlignCenter)

        layout.addWidget(self.trails_container)

        # 添加弹性空间
        layout.addStretch()

        # 淡出动画
        self._opacity = 1.0
        self.fade_animation = QPropertyAnimation(self, b"opacity")
        self.fade_animation.setDuration(400)
        self.fade_animation.setEasingCurve(QEasingCurve.OutCubic)

    def set_state(self, state: LoaderState):
        """设置加载器状态"""
        self._state = state

        if state == LoaderState.INTERMEDIATE:
            # 中间状态：隐藏拖尾和间距，启用循环扫描模式
            self.trails_container.hide()
            self.spacing_widget.hide()
            self.scanner.set_intermediate_mode(True)
        else:
            # 运行中状态：显示拖尾和间距，使用单向扫描模式
            self.trails_container.show()
            self.spacing_widget.show()
            self.scanner.set_intermediate_mode(False)

    def set_status_color(self, status: StatusColor):
        """设置状态颜色（红/黄/绿）"""
        self.scanner.set_status_color(status)

    def get_state(self) -> LoaderState:
        """获取当前状态"""
        return self._state

    def start_animation(self):
        """启动统一动画定时器"""
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._update_animation)
        self.animation_timer.start(16)  # 约60fps，1.2秒周期

    def _update_animation(self):
        """统一更新扫描器和拖尾的动画进度"""
        # 1.2秒周期，每帧16ms
        self.animation_progress += 16 / 1200.0
        if self.animation_progress >= 1.0:
            self.animation_progress = 0.0

        # 同步更新两个组件
        self.scanner.set_animation_progress(self.animation_progress)
        self.trails.set_animation_progress(self.animation_progress)

        # 更新状态呼吸动画（如果有状态颜色）
        # 使用独立的时间累加器，避免重置问题
        if not hasattr(self, '_status_breath_time'):
            self._status_breath_time = 0.0

        self._status_breath_time += 16 / 1000.0  # 秒为单位
        if self._status_breath_time >= 2.0:  # 2秒周期
            self._status_breath_time = 0.0

        # 将时间映射到 0-1 的呼吸周期
        breath_progress = self._status_breath_time / 2.0
        self.scanner.set_status_breath(breath_progress)

    def get_opacity(self):
        return self._opacity

    def set_opacity(self, value):
        self._opacity = value
        # 使用 setWindowOpacity 来实现整体透明度
        if self.parent():
            self.setWindowOpacity(value)

    opacity = Property(float, get_opacity, set_opacity)

    def fade_out(self):
        """淡出并移除"""
        self.fade_animation.setStartValue(1.0)
        self.fade_animation.setEndValue(0.0)
        self.fade_animation.finished.connect(self._on_fade_finished)
        self.fade_animation.start()

    def _on_fade_finished(self):
        """淡出完成后的处理"""
        self.hide()
        if self.parent():
            self.deleteLater()