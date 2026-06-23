"""
LANSyncBox 主入口
"""
import sys
import os
import ctypes

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QFont

from ui.main_window import MainWindow
from i18n import I18n
from config import Config, UserConfig


def get_resource_path(relative_path):
    """获取资源文件路径，兼容打包和未打包"""
    try:
        base_path = sys._MEIPASS  # PyInstaller打包后的临时目录
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def main():
    """主函数"""
    # 设置AppUserModelID（必须在QApplication创建之前）
    if sys.platform == 'win32':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(Config.APP_NAME)
        except (AttributeError, OSError):
            pass
    
    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName(Config.APP_NAME)
    app.setApplicationVersion(Config.APP_VERSION)
    app.setOrganizationName(Config.APP_AUTHOR)
    
    # 设置默认字体
    font = QFont("Microsoft YaHei UI", 10)
    app.setFont(font)
    
    # 设置程序图标
    icon_path = get_resource_path('icon.ico')
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))
    
    # 设置默认语言（从 config.json 加载用户偏好）
    I18n.set_language(UserConfig.get_language())
    
    # 创建主窗口
    window = MainWindow()
    if os.path.exists(icon_path):
        window.setWindowIcon(QIcon(icon_path))
    window.show()
    
    # Windows任务栏图标设置
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
    
    # 运行应用
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
