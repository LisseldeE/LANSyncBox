"""
LANSyncBox 配置文件
"""
import os
import sys
import json
from pathlib import Path


class Config:
    """应用配置"""

    # 应用信息
    APP_NAME = "LANSyncBox"
    APP_VERSION = "R5"
    APP_AUTHOR = "Lisselde_E"
    APP_EMAIL = "Lisselde.E@outlook.com"

    # 功能开关
    # 检查更新按钮：True=显示（GitHub 版本），False=隐藏（微软商店版本）
    ENABLE_CHECK_UPDATE = False

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
    def get_app_dir() -> Path:
        """获取应用程序所在目录"""
        # 判断是否在打包环境中运行
        if getattr(sys, 'frozen', False):
            # 打包后：使用exe所在目录
            return Path(sys.executable).parent
        else:
            # 开发环境：使用脚本所在目录
            return Path(__file__).parent

    @staticmethod
    def get_downloads_folder() -> Path:
        """获取 Windows 下载文件夹路径（动态获取，应对用户修改默认位置）"""
        if sys.platform == 'win32':
            try:
                import ctypes
                from ctypes import wintypes

                # FOLDERID_Downloads GUID: {374DE290-123F-4565-9164-39C4925E467B}
                # GUID 结构体字节序：Data1(4B LE) + Data2(2B LE) + Data3(2B LE) + Data4(8B BE)
                guid_bytes = (ctypes.c_byte * 16)(
                    0x90, 0xE2, 0x4D, 0x37,  # Data1 (LE)
                    0x3F, 0x12,              # Data2 (LE)
                    0x65, 0x45,              # Data3 (LE)
                    0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B  # Data4 (BE)
                )

                shgfp = ctypes.windll.shell32.SHGetKnownFolderPath
                shgfp.restype = ctypes.c_long
                shgfp.argtypes = [
                    ctypes.POINTER(ctypes.c_byte * 16),
                    wintypes.DWORD,
                    wintypes.HANDLE,
                    ctypes.POINTER(wintypes.LPWSTR)
                ]

                path_ptr = wintypes.LPWSTR()
                # KF_FLAG_DEFAULT = 0
                result = shgfp(ctypes.byref(guid_bytes), 0, None, ctypes.byref(path_ptr))

                if result == 0:  # S_OK
                    path = path_ptr.value
                    ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                    return Path(path)
            except Exception:
                pass

        # Fallback: 用户主目录下的 Downloads
        return Path.home() / "Downloads"

    @staticmethod
    def get_sync_folder() -> Path:
        """获取同步文件夹路径（位于系统下载目录下）"""
        downloads = Config.get_downloads_folder()
        sync_folder = downloads / Config.SYNC_FOLDER_NAME
        sync_folder.mkdir(exist_ok=True)
        return sync_folder

    @staticmethod
    def get_room_folder(room_code: str) -> Path:
        """获取指定房间的同步文件夹"""
        sync_folder = Config.get_sync_folder()
        room_folder = sync_folder / room_code
        room_folder.mkdir(exist_ok=True)
        return room_folder

    @staticmethod
    def get_preview_folder(room_code: str = "") -> Path:
        """获取预览文件夹路径（用于只读打开文件）
        Args:
            room_code: 房间号，可选。如果提供，则创建房间专属预览子目录
        Returns:
            预览文件夹路径
        """
        sync_folder = Config.get_sync_folder()
        preview_folder = sync_folder / "preview"
        if room_code:
            preview_folder = preview_folder / room_code
        preview_folder.mkdir(parents=True, exist_ok=True)
        return preview_folder


class UserConfig:
    """用户配置管理（持久化到 config.json）"""

    _config_path: Path = None
    _config_data: dict = None

    @classmethod
    def _get_config_path(cls) -> Path:
        """获取配置文件路径"""
        if cls._config_path is None:
            cls._config_path = Config.get_app_dir() / "config.json"
        return cls._config_path

    @classmethod
    def load(cls) -> dict:
        """加载配置（带默认值合并）"""
        if cls._config_data is not None:
            return cls._config_data

        default_config = {
            "language": "zh_CN",
            "fixed_room_code_enabled": False,
            "fixed_room_code": ""
        }

        config_path = cls._get_config_path()
        if config_path.exists():
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                # 合并默认值，防止旧配置缺字段
                for key, value in default_config.items():
                    if key not in data:
                        data[key] = value
                cls._config_data = data
            except (json.JSONDecodeError, IOError, OSError):
                cls._config_data = default_config
        else:
            cls._config_data = default_config

        return cls._config_data

    @classmethod
    def save(cls):
        """保存配置到 config.json"""
        if cls._config_data is None:
            return
        config_path = cls._get_config_path()
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(cls._config_data, f, ensure_ascii=False, indent=2)
        except (IOError, OSError):
            pass

    @classmethod
    def get(cls, key: str, default=None):
        """获取配置项"""
        data = cls.load()
        return data.get(key, default)

    @classmethod
    def set(cls, key: str, value):
        """设置配置项并立即保存"""
        data = cls.load()
        data[key] = value
        cls.save()

    @classmethod
    def get_language(cls) -> str:
        """获取语言设置"""
        return cls.get("language", "zh_CN")

    @classmethod
    def set_language(cls, lang: str):
        """设置语言"""
        cls.set("language", lang)

    @classmethod
    def get_fixed_room_code_enabled(cls) -> bool:
        """获取固定房间号启用状态"""
        return bool(cls.get("fixed_room_code_enabled", False))

    @classmethod
    def set_fixed_room_code_enabled(cls, enabled: bool):
        """设置固定房间号启用状态"""
        cls.set("fixed_room_code_enabled", bool(enabled))

    @classmethod
    def get_fixed_room_code(cls) -> str:
        """获取固定的房间号"""
        return cls.get("fixed_room_code", "")

    @classmethod
    def set_fixed_room_code(cls, code: str):
        """设置固定的房间号"""
        cls.set("fixed_room_code", code)