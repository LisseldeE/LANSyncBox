# -*- coding: utf-8 -*-
"""
LANSyncBox 配置文件
定义全局常量和配置项
"""

# 应用信息
APP_NAME = "LANSyncBox"
APP_VERSION = "R2"
APP_AUTHOR = "Lisselde_E"
APP_EMAIL = "Lisselde.E@outlook.com"
APP_REPO = "LisseldeE/LANSyncBox"  # GitHub 仓库
APP_REPO_GITEE = "Lisselde_E/LANSyncBox"  # Gitee 仓库（用户名可能不同）
APP_ID = "LANSyncBox.LANSyncBox.R2"

# API 端点
GITHUB_API = f"https://api.github.com/repos/{APP_REPO}/tags"
GITEE_API = f"https://gitee.com/api/v5/repos/{APP_REPO_GITEE}/tags"
GITHUB_RELEASES = f"https://github.com/{APP_REPO}/releases"
GITEE_RELEASES = f"https://gitee.com/{APP_REPO_GITEE}/releases"

# 网络配置
DEFAULT_PORT = 9527
BUFFER_SIZE = 65536  # 64KB缓冲区
MAX_CONCURRENT_TRANSFERS = 3  # 最大同时传输数

# 房间配置
ROOM_CODE_LENGTH = 6  # 房间号长度

# 协议消息类型
MSG_TYPE_FILE = 0x01          # 文件传输
MSG_TYPE_DELETE = 0x02         # 删除指令
MSG_TYPE_AUTH_REQ = 0x03       # 房间验证请求
MSG_TYPE_AUTH_RESP = 0x04      # 房间验证响应
MSG_TYPE_FILE_LIST_REQ = 0x05  # 文件列表请求
MSG_TYPE_FILE_LIST_RESP = 0x06 # 文件列表响应
MSG_TYPE_HEARTBEAT = 0x07      # 心跳包
MSG_TYPE_FULL_SYNC_REQ = 0x08  # 全量同步请求
MSG_TYPE_FULL_SYNC_RESP = 0x09 # 全量同步响应
MSG_TYPE_CLIENT_INFO = 0x0A    # 客户端信息更新
MSG_TYPE_DIR_CREATE = 0x0B     # 目录创建

# 超时设置
CONNECTION_TIMEOUT = 30  # 连接超时（秒）- 增加到30秒
HEARTBEAT_INTERVAL = 15  # 心跳间隔（秒）- 减少到15秒更频繁
HEARTBEAT_TIMEOUT = 45  # 心跳超时（秒）- 3次心跳未响应则断开

# 文件监控
WATCHDOG_DEBOUNCE_SECONDS = 1  # 文件变更防抖时间

# UI样式
STYLESHEET = """
/* 主窗口背景 */
QMainWindow {
    background-color: #f8f9fa;
}

/* 分组框 */
QGroupBox {
    font-weight: bold;
    font-size: 13px;
    font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    border: 1px solid #dee2e6;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 8px;
    background-color: white;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 8px;
    color: #495057;
}

/* 输入框 */
QLineEdit {
    padding: 10px 12px;
    border: 1px solid #ced4da;
    border-radius: 6px;
    background-color: white;
    font-size: 13px;
    font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
}
QLineEdit:focus {
    border: 2px solid #4dabf7;
}

/* 默认按钮 */
QPushButton {
    padding: 10px 20px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
    background-color: #e9ecef;
    color: #495057;
    border: 1px solid #ced4da;
}
QPushButton:hover {
    background-color: #dee2e6;
}
QPushButton:disabled {
    background-color: #adb5bd;
    color: #868e96;
}

/* 主要操作按钮（蓝色） */
QPushButton#primaryBtn {
    background-color: #339af0;
    color: white;
    border: none;
}
QPushButton#primaryBtn:hover {
    background-color: #228be6;
}
QPushButton#primaryBtn:disabled {
    background-color: #adb5bd;
}

/* 确认按钮（绿色） */
QPushButton#successBtn {
    background-color: #51cf66;
    color: white;
    border: none;
}
QPushButton#successBtn:hover {
    background-color: #40c057;
}
QPushButton#successBtn:disabled {
    background-color: #adb5bd;
}

/* 危险按钮（红色） */
QPushButton#dangerBtn {
    background-color: #ff6b6b;
    color: white;
    border: none;
}
QPushButton#dangerBtn:hover {
    background-color: #fa5252;
}

/* 下拉框 */
QComboBox {
    background-color: #e9ecef;
    color: #495057;
    border: 1px solid #ced4da;
    border-radius: 6px;
    padding: 8px;
    font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
}
QComboBox:hover {
    background-color: #dee2e6;
    border: 1px solid #adb5bd;
}
QComboBox::drop-down {
    border: none;
    width: 30px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 5px solid transparent;
    border-right: 5px solid transparent;
    border-top: 5px solid #495057;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background-color: white;
    border: 1px solid #ced4da;
    selection-background-color: #339af0;
    selection-color: white;
}

/* 表格 */
QTableWidget {
    border: 1px solid #dee2e6;
    border-radius: 8px;
    background-color: white;
    gridline-color: #e9ecef;
    font-family: "Microsoft YaHei", "Segoe UI", Arial, sans-serif;
}
QTableWidget::item {
    padding: 8px;
}
QHeaderView::section {
    background-color: #f1f3f5;
    padding: 10px;
    border: none;
    border-bottom: 1px solid #dee2e6;
    font-weight: 600;
    color: #495057;
}

/* 滚动条 */
QScrollBar:vertical {
    border: none;
    background-color: #f1f3f5;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background-color: #ced4da;
    min-height: 30px;
    border-radius: 5px;
}
QScrollBar::handle:vertical:hover {
    background-color: #adb5bd;
}

/* 进度条 */
QProgressBar {
    border: 1px solid #ced4da;
    border-radius: 4px;
    text-align: center;
    background-color: #e9ecef;
}
QProgressBar::chunk {
    background-color: #51cf66;
    border-radius: 3px;
}
"""

# 颜色定义
COLORS = {
    'primary': '#339af0',
    'primary_hover': '#228be6',
    'success': '#51cf66',
    'success_hover': '#40c057',
    'danger': '#ff6b6b',
    'danger_hover': '#fa5252',
    'background': '#f8f9fa',
    'card': '#ffffff',
    'border': '#dee2e6',
    'disabled': '#adb5bd',
    'text_primary': '#495057',
    'text_secondary': '#868e96',
    'focus': '#4dabf7',
}