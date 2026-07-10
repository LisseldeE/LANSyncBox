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
    APP_VERSION = "R6.6"             # 内部版本号（开源直装版显示 + 检查更新比较用）
    STORE_VERSION = "6.6.0.0"      # 微软商店版本号（四段式，符合 MSIX 打包要求）
    APP_AUTHOR = "Lisselde_E"
    APP_EMAIL = "Lisselde.E@outlook.com"

    # 功能开关
    # 检查更新按钮：True=显示（开源直装版），False=隐藏（微软商店版本）
    ENABLE_CHECK_UPDATE = True

    # 显示用版本号：根据发布渠道动态选择
    # 开源直装版（ENABLE_CHECK_UPDATE=True）→ APP_VERSION（R5）
    # 微软商店版（ENABLE_CHECK_UPDATE=False）→ STORE_VERSION（5.3.0.0）
    DISPLAY_VERSION = STORE_VERSION if not ENABLE_CHECK_UPDATE else APP_VERSION

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
    def get_real_appdata() -> Path:
        """获取用户数据存储路径（避开MSIX虚拟化）

        MSIX虚拟化会对AppData\\Roaming路径进行重定向，即使manifest设置为mediumIL。
        为了彻底解决虚拟化问题，使用用户主目录下的独立文件夹。

        Returns:
            Path: 用户主目录下的LANSyncBox文件夹
        """
        if sys.platform != 'win32':
            # 非 Windows 平台：直接返回 ~/.config 或 XDG_CONFIG_HOME
            xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
            return Path(xdg)

        # Windows平台：使用用户主目录下的独立文件夹（避开MSIX虚拟化）
        # 文件位置：C:\\Users\\<用户>\\LANSyncBox\\
        # 这个路径不受MSIX文件系统虚拟化影响
        return Path.home() / 'LANSyncBox'

    @staticmethod
    def get_data_dir() -> Path:
        """获取用户数据目录（用户主目录\\LANSyncBox），用于存放用户配置

        注意：使用用户主目录下的独立文件夹（避开MSIX虚拟化），确保外部程序也能访问。
        """
        appdata = Config.get_real_appdata()
        data_dir = appdata  # 已经是用户主目录下的LANSyncBox文件夹
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    @staticmethod
    def get_data_dir_path_only() -> Path:
        """获取用户数据目录路径（不创建文件夹）

        用于UI显示预期路径，避免触发文件系统操作导致窗口闪烁。

        注意：使用用户主目录下的独立文件夹（避开MSIX虚拟化），确保外部程序也能访问。
        """
        return Config.get_real_appdata()  # 已经是用户主目录下的LANSyncBox文件夹

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
        """
        获取同步文件夹路径（位于用户主目录\\LANSyncBox\\SyncFolder）
        """
        # 使用用户主目录下的LANSyncBox作为基础路径
        data_dir = Config.get_data_dir()

        # 同步文件夹位于: C:\\Users\\<用户>\\LANSyncBox\\SyncFolder
        sync_folder = data_dir / Config.SYNC_FOLDER_NAME
        sync_folder.mkdir(parents=True, exist_ok=True)

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
            room_code: 房间号（可选），用于区分不同房间的预览文件
        Returns:
            预览文件夹路径
        """
        sync_folder = Config.get_sync_folder()
        preview_folder = sync_folder / "preview"
        if room_code:
            preview_folder = preview_folder / room_code
        preview_folder.mkdir(parents=True, exist_ok=True)
        return preview_folder

    @staticmethod
    def get_cache_size() -> int:
        """计算缓存目录总大小（字节）
        Returns:
            缓存目录大小（字节），如果目录不存在返回 0
        """
        sync_folder = Config.get_sync_folder()
        if not sync_folder.exists():
            return 0

        total_size = 0
        try:
            for dirpath, dirnames, filenames in os.walk(sync_folder):
                for filename in filenames:
                    filepath = os.path.join(dirpath, filename)
                    # 忽略符号链接等特殊情况
                    if os.path.isfile(filepath):
                        try:
                            total_size += os.path.getsize(filepath)
                        except (OSError, PermissionError):
                            # 单个文件访问失败,跳过继续计算
                            pass
        except (OSError, PermissionError):
            # 遍历失败时返回 0
            pass

        return total_size


class UserConfig:
    """用户配置管理（持久化到 config.json）"""

    _config_path: Path = None
    _config_data: dict = None

    @classmethod
    def _get_config_path(cls) -> Path:
        """获取配置文件路径（位于用户主目录\\LANSyncBox\\config.json）
        注意：此方法不创建文件夹，避免在加载配置时触发文件系统操作。
        使用用户主目录下的独立文件夹（避开MSIX虚拟化），确保配置文件与同步文件夹在同一位置。"""
        if cls._config_path is None:
            # 使用用户主目录下的独立文件夹（避开MSIX虚拟化）
            appdata = Config.get_real_appdata()  # 已经包含LANSyncBox
            cls._config_path = appdata / "config.json"  # 不再添加APP_NAME
        return cls._config_path

    @classmethod
    def _migrate_if_needed(cls):
        """旧版迁移逻辑已移除，不再处理程序目录下的 config.json"""
        # 新路径已有配置，无需处理
        if cls._get_config_path().exists():
            return

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

        # 首次加载时尝试从旧路径迁移配置
        cls._migrate_if_needed()

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
            # 确保配置文件所在目录存在（只在首次保存时创建）
            config_path.parent.mkdir(parents=True, exist_ok=True)
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