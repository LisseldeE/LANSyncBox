"""
LANSyncBox 配置文件
"""
import os
from pathlib import Path


class Config:
    """应用配置"""
    
    # 应用信息
    APP_NAME = "LANSyncBox"
    APP_VERSION = "R2"
    APP_AUTHOR = "Lisselde_E"
    APP_EMAIL = "Lisselde.E@outlook.com"
    
    # 仓库信息
    GITHUB_REPO = "LisseldeE/LANSyncBox"
    GITEE_REPO = "Lisselde_E/LANSyncBox"
    
    # API 端点
    GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
    GITEE_API = f"https://gitee.com/api/v5/repos/{GITEE_REPO}/tags"
    GITHUB_RELEASES = f"https://github.com/{GITHUB_REPO}/releases"
    GITEE_RELEASES = f"https://gitee.com/{GITEE_REPO}/releases"
    
    # 默认同步文件夹
    SYNC_FOLDER_NAME = "SyncFolder"
    
    # 房间号配置
    ROOM_CODE_LENGTH = 6
    ROOM_CODE_MIN = 100000
    ROOM_CODE_MAX = 999999
    
    # 网络配置
    DEFAULT_PORT = 9527
    BUFFER_SIZE = 65536  # 64KB
    MAX_FILE_SIZE = 1024 * 1024 * 1024  # 1GB
    
    # 同步配置
    SYNC_INTERVAL = 1.0  # 秒
    MAX_RETRY_COUNT = 3
    RETRY_DELAY = 2.0  # 秒
    
    # 文件操作配置
    MAX_FILE_NAME_LENGTH = 255
    FORBIDDEN_CHARS = ['<', '>', ':', '"', '|', '?', '*']
    
    # UI配置
    WINDOW_MIN_WIDTH = 900
    WINDOW_MIN_HEIGHT = 600
    FILE_LIST_ROW_HEIGHT = 30
    
    @staticmethod
    def get_sync_folder() -> Path:
        """获取同步文件夹路径"""
        # 程序所在目录下的 SyncFolder
        base_path = Path(__file__).parent
        sync_folder = base_path / Config.SYNC_FOLDER_NAME
        sync_folder.mkdir(exist_ok=True)
        return sync_folder
    
    @staticmethod
    def get_room_folder(room_code: str) -> Path:
        """获取指定房间的同步文件夹"""
        sync_folder = Config.get_sync_folder()
        room_folder = sync_folder / room_code
        room_folder.mkdir(exist_ok=True)
        return room_folder