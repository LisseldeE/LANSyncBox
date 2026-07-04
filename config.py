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
    APP_VERSION = "R6.3"             # 内部版本号（开源直装版显示 + 检查更新比较用）
    STORE_VERSION = "6.3.0.0"      # 微软商店版本号（四段式，符合 MSIX 打包要求）
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
        """获取真实的、未被MSIX虚拟化的AppData\\Roaming路径

        使用Windows API SHGetKnownFolderPath + KF_FLAG_NO_PACKAGE_REDIRECTION标志，
        确保在MSIX环境下也能获取真实路径，避免文件系统虚拟化导致的路径不一致问题。

        普通exe模式下，此方法返回标准AppData路径（兼容性良好）。

        Returns:
            Path: 真实的AppData\\Roaming路径
        """
        if sys.platform != 'win32':
            # 非 Windows 平台：直接返回 ~/.config 或 XDG_CONFIG_HOME
            xdg = os.environ.get('XDG_CONFIG_HOME', str(Path.home() / '.config'))
            return Path(xdg)

        try:
            # Windows平台：使用SHGetKnownFolderPath获取真实路径
            import ctypes
            from ctypes import wintypes

            # FOLDERID_RoamingAppData GUID
            # {3EB685DB-65F9-4CF6-A03A-E3EF65729F3D}
            FOLDERID_RoamingAppData = ctypes.c_byte(16)
            guid_bytes = bytes([
                0xDB, 0x85, 0xB6, 0x3E,  # Data1 (little endian)
                0xF9, 0x65,              # Data2 (little endian)
                0xF6, 0x4C,              # Data3 (little endian)
                0xA0, 0x3A,              # Data4[0-1]
                0xE3, 0xEF, 0x65, 0x72, 0x9F, 0x3D  # Data4[2-7]
            ])
            guid = (ctypes.c_byte * 16)(*guid_bytes)

            # KF_FLAG_NO_PACKAGE_REDIRECTION = 0x10000
            # 此标志确保在MSIX环境下获取真实路径，而非虚拟化路径
            KF_FLAG_NO_PACKAGE_REDIRECTION = 0x10000

            # 调用 SHGetKnownFolderPath
            # HRESULT SHGetKnownFolderPath(
            #   REFKNOWNFOLDERID rfid,
            #   DWORD dwFlags,
            #   HANDLE hToken,
            #   PWSTR *ppszPath
            # );
            path_ptr = wintypes.LPWSTR()
            result = ctypes.windll.shell32.SHGetKnownFolderPath(
                guid,
                KF_FLAG_NO_PACKAGE_REDIRECTION,
                None,  # hToken = NULL (当前用户)
                ctypes.byref(path_ptr)
            )

            if result == 0:  # S_OK
                # 成功获取路径
                path = Path(path_ptr.value)
                # 释放内存（CoTaskMemFree）
                ctypes.windll.ole32.CoTaskMemFree(path_ptr)
                return path
            else:
                # API调用失败，fallback到环境变量
                return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming')))

        except Exception:
            # 异常情况（如API不可用），fallback到环境变量
            return Path(os.environ.get('APPDATA', str(Path.home() / 'AppData' / 'Roaming')))

    @staticmethod
    def get_data_dir() -> Path:
        """获取用户数据目录（AppData\\Roaming\\LANSyncBox），用于存放用户配置

        注意：使用真实路径（避免MSIX虚拟化），确保外部程序也能访问。
        """
        appdata = Config.get_real_appdata()
        data_dir = appdata / Config.APP_NAME
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    @staticmethod
    def get_data_dir_path_only() -> Path:
        """获取用户数据目录路径（不创建文件夹）

        用于UI显示预期路径，避免触发文件系统操作导致窗口闪烁。

        注意：使用真实路径（避免MSIX虚拟化），确保外部程序也能访问。
        """
        appdata = Config.get_real_appdata()
        return appdata / Config.APP_NAME

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
        获取同步文件夹路径（位于用户数据目录下）
        """
        # 使用AppData\Roaming\LANSyncBox作为基础路径
        data_dir = Config.get_data_dir()

        # 同步文件夹位于: AppData\Roaming\LANSyncBox\SyncFolder
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
        """获取配置文件路径（位于 AppData\\Roaming\\LANSyncBox\\config.json）
        注意：此方法不创建文件夹，避免在加载配置时触发文件系统操作。
        使用真实路径（避免MSIX虚拟化），确保配置文件与同步文件夹在同一位置。"""
        if cls._config_path is None:
            # 使用真实路径（避免MSIX虚拟化导致的脑裂问题）
            appdata = Config.get_real_appdata()
            cls._config_path = appdata / Config.APP_NAME / "config.json"
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