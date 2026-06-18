# -*- coding: utf-8 -*-
"""
LANSyncBox 主入口文件
局域网文件同步工具
"""

import sys
import os
import ctypes

# 确保可以导入其他模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QtGui import QIcon, QFont
from PyQt5.QtCore import Qt

from config import APP_NAME, APP_VERSION, APP_ID
from ui.main_window import MainWindow


def get_resource_path(relative_path):
    """获取资源文件路径，兼容打包和未打包"""
    try:
        base_path = sys._MEIPASS  # PyInstaller打包后的临时目录
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def setup_dpi_scaling():
    """设置DPI缩放适配（解决高DPI显示问题）"""
    # Windows高DPI适配 - 启用Per-Monitor DPI感知
    if sys.platform == 'win32':
        try:
            # 设置为 Per Monitor Aware V1 (1)，让程序自己处理DPI缩放
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            # 如果失败，尝试使用旧版本API
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass
    
    # Qt高DPI设置 - 启用自动缩放
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    
    # 适配125%、150%等非整数倍缩放
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )


def main():
    """主函数"""
    # 1. DPI适配（必须在QApplication创建之前）
    setup_dpi_scaling()
    
    # 2. 设置AppUserModelID（必须在QApplication创建之前）
    if sys.platform == 'win32':
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    
    app = QApplication(sys.argv)
    
    # 3. 设置应用程序信息
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("LANSyncBox")
    
    # 4. 设置默认字体（确保中文显示正常）
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    
    # 5. 设置窗口图标
    icon_path = get_resource_path('icon.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    window = MainWindow()
    if os.path.exists(icon_path):
        window.setWindowIcon(QIcon(icon_path))
    window.show()
    
    # 6. Windows任务栏图标设置（必须在窗口显示后）
    if sys.platform == 'win32' and os.path.exists(icon_path):
        try:
            hwnd = int(window.winId())
            hicon = ctypes.windll.user32.LoadImageW(
                None, icon_path, 1,  # IMAGE_ICON
                0, 0, 0x10  # LR_LOADFROMFILE
            )
            if hicon:
                ctypes.windll.user32.SendMessageW(hwnd, 0x80, 0, hicon)  # WM_SETICON, ICON_SMALL
                ctypes.windll.user32.SendMessageW(hwnd, 0x80, 1, hicon)  # WM_SETICON, ICON_BIG
        except Exception:
            pass
    
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()