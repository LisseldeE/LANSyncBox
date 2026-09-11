"""
LANSyncBox Pro 主入口
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import sys
import os
import ctypes

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QFont, QPalette, QGuiApplication
from PySide6.QtNetwork import QLocalSocket, QLocalServer

from ui.main_window import MainWindow
from i18n import I18n
from config import Config, UserConfig

# 单实例命名（Windows 命名管道 / Linux、macOS 域套接字，按登录会话隔离）
SINGLE_INSTANCE_NAME = Config.APP_NAME


def get_resource_path(relative_path):
    """获取资源文件路径，兼容打包和未打包"""
    try:
        base_path = sys._MEIPASS  # PyInstaller打包后的临时目录
    except AttributeError:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)


def _activate_existing_instance() -> bool:
    """请求已有实例唤出主窗口；返回 True 表示已有实例在运行（本进程应退出）。"""
    sock = QLocalSocket()
    sock.connectToServer(SINGLE_INSTANCE_NAME)
    if sock.waitForConnected(400):
        sock.write(b"show")
        sock.flush()
        sock.waitForBytesWritten(400)
        sock.disconnectFromServer()
        return True
    return False


def _setup_single_instance(window):
    """作为首个实例监听单实例消息，收到 'show' 时唤出主窗口。

    返回监听中的 QLocalServer；返回 None 表示已有实例占用（调用方应退出）。
    """
    # 清理上次异常退出可能残留的域套接字文件（Windows 上无副作用）
    QLocalServer.removeServer(SINGLE_INSTANCE_NAME)
    server = QLocalServer()
    if not server.listen(SINGLE_INSTANCE_NAME):
        # 竞态：已有实例刚完成监听。再尝试一次激活；仍失败则保守继续以当前实例运行
        if _activate_existing_instance():
            return None
        return server  # listen 失败但无法激活：继续运行（回退多开，避免锁死）
    else:
        def _on_new_connection():
            conn = server.nextPendingConnection()
            if conn:
                conn.waitForReadyRead(400)
                data = bytes(conn.readAll())
                conn.disconnectFromServer()
                if data == b"show":
                    # 唤起当前可见的活动窗口（同步界面优先，避免空白主窗口再次呼出）
                    window.activate_visible_window()
        server.newConnection.connect(_on_new_connection)
        return server


def main():
    """主函数"""
    # 写入版本信息到配置文件（仅在 ENABLE_CHECK_UPDATE=True 时）
    if Config.ENABLE_CHECK_UPDATE:
        try:
            # 获取当前可执行文件路径
            if getattr(sys, 'frozen', False):
                # PyInstaller 打包：可执行文件完整路径
                exe_path = sys.executable
            else:
                # 开发环境：主脚本路径
                exe_path = os.path.abspath(__file__)

            UserConfig.update_reference_info(exe_path)
        except Exception:
            pass  # 静默失败，不影响程序启动

    # 设置AppUserModelID（必须在QApplication创建之前）
    if Config.IS_WINDOWS:
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(Config.APP_NAME)
        except (AttributeError, OSError):
            pass

    # 使用 PassThrough 策略处理非整数缩放（如125%），避免边框被位图放大裁切
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    # 创建应用
    app = QApplication(sys.argv)
    app.setApplicationName(Config.APP_NAME)
    app.setApplicationVersion(Config.APP_VERSION)
    app.setOrganizationName(Config.APP_AUTHOR)

    # 单实例保护：已有实例在运行时唤出其主窗口，本进程直接退出
    if _activate_existing_instance():
        return 0

    # 全局：点击输入框以外位置自动取消输入焦点
    from ui.widgets import install_click_away_focus
    install_click_away_focus(app)
    
    # 设置默认字体（按平台选择，配合手动切换平台时自动匹配中文显示字体）
    font_family = {
        'w': "Microsoft YaHei UI",
        'l': "Noto Sans CJK SC",
        'm': "PingFang SC",
    }.get(Config.PLATFORM, "Microsoft YaHei UI")
    # 若系统缺失该字体，Qt 会自动回退到默认字体，避免中文显示为方框
    app.setFont(QFont(font_family, 10))
    
    # Linux：GNOME 合成器对程序窗口投影较弱/缺失，为顶层窗口加淡边线以与桌面背景区分
    # 边线颜色依据应用调色板 Window 角色亮度自适应深/浅色模式（与 SnapOutlineButton 判定一致）
    if Config.IS_LINUX:
        bg = app.palette().color(QPalette.Window)
        luminance = bg.red() * 0.299 + bg.green() * 0.587 + bg.blue() * 0.114
        border_color = "#3f3f3f" if luminance < 128 else "#c4c4c4"
        # 对 QMainWindow / QDialog 两类顶层窗口统一加 1px 直角边框；子控件不受影响
        app.setStyleSheet(f"QMainWindow, QDialog {{ border: 1px solid {border_color}; }}")
    
    # 设置程序图标（Linux 用 icon.png，其余用 icon.ico）
    icon_name = 'icon.png' if Config.IS_LINUX else 'icon.ico'
    icon_path = get_resource_path(icon_name)
    if os.path.exists(icon_path):
        app.setWindowIcon(QIcon(icon_path))

    # Linux：关联 .desktop 文件，使 GNOME 任务栏/程序坞按 .desktop 的 icon 显示
    if Config.IS_LINUX:
        QGuiApplication.setDesktopFileName(f"{Config.APP_NAME}.desktop")
    
    # 设置默认语言（从 config.json 加载用户偏好）
    I18n.set_language(UserConfig.get_language())

    # 创建主窗口
    window = MainWindow()
    if os.path.exists(icon_path):
        window.setWindowIcon(QIcon(icon_path))
    window.show()

    # 单实例服务：监听后续启动的唤出请求（须持有引用防 GC）
    single_instance_server = _setup_single_instance(window)
    if single_instance_server is None:
        # 竞态下已被其他实例占用且已成功唤出：本进程退出
        return 0
    
    # Windows任务栏图标设置
    if Config.IS_WINDOWS and os.path.exists(icon_path):
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
