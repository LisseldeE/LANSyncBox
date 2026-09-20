"""
LANSyncBox 配置文件
Copyright (c) 2026 Lisselde_E <Lisselde.E@outlook.com>.
Licensed under the GNU General Public License v3.0.
"""
import os
import sys
import json
from pathlib import Path


class Config:
    """应用配置"""

    # 应用信息
    APP_NAME = "LANSyncBox Pro"
    APP_VERSION = "R1.1.0.0"
    # 同步逻辑版本号
    SYNC_LOGIC_VERSION = "26.9C1"
    APP_SERIAL = "P269.WH"
    APP_SERIAL_FULL = ".".join(x for x in (APP_NAME, APP_VERSION, APP_SERIAL) if x)
    APP_VERSION_SERIAL = ".".join(x for x in (APP_VERSION, APP_SERIAL) if x)
    APP_AUTHOR = "Lisselde_E"
    APP_AUTHOR_LINK = "https://lisseldee.github.io/#1"  # 作者主页链接

    # 运行平台（单个手动变量，跨平台调试时直接改此值即可切换分支，不自动检测）
    PLATFORM = 'w'

    # 平台派生布尔标志，供各处分支使用（勿手动改，由上方 PLATFORM 推导）
    IS_WINDOWS = PLATFORM == 'w'
    IS_LINUX = PLATFORM == 'l'
    IS_MACOS = PLATFORM == 'm'

    # 功能开关
    # 检查更新按钮：True=显示（开源直装版），False=隐藏（微软商店版本）
    ENABLE_CHECK_UPDATE = True

    # 仓库信息
    GITHUB_REPO = "LisseldeE/LANSyncBox"
    GITEE_REPO = "Lisselde_E/LANSyncBox"

    # 版本号托管于 GitHub Pages 纯文本文件，避免 raw 外链滥用/API tags 频率限制
    UPDATE_URL = "https://lisseldee.github.io/version/lansyncboxpro"
    # 公告文件：首行版本号（如 26.9.15.1），其后为公告正文
    ANNOUNCEMENT_URL = "https://lisseldee.github.io/announcement/lansyncboxpro"
    # 下载落地页（按语言区分，保持不变）
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
        """获取用户数据存储路径

        各平台统一使用用户主目录下的 LANSyncBox 文件夹：
        - Windows: ~/LANSyncBox（避开 MSIX 虚拟化重定向）
        - Linux:   ~/LANSyncBox（与 Windows 保持一致，便于跨平台共用同步逻辑）
        - macOS:   ~/LANSyncBox（暂未单独实现，沿用统一路径）

        Returns:
            Path: 用户主目录下的 LANSyncBox 文件夹
        """
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
        if Config.IS_WINDOWS:
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
                # 忽略 preview 预览文件夹（不算缓存占用）
                dirnames[:] = [d for d in dirnames if d != "preview"]
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
            "fixed_room_code": "",
            "clean_cache_enabled": False,
            "confirm_leave_no_ask": False,
            "auto_check_update": False,
            "receive_announcements": True,
            "last_announcement": "",
            "last_announcement_text": "",
            "end_id": "",
            "room_history": [],
            "default_perm": "rw",
            "room_perm": {}  # 每房间最近一次主机下发的本端权限档位（主机离线期间保持只读不放松）
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
        tmp_path = None
        try:
            # 确保配置文件所在目录存在（只在首次保存时创建）
            config_path.parent.mkdir(parents=True, exist_ok=True)
            # 原子写：先写临时文件再 os.replace，进程被杀/断电也不会写坏 config.json
            tmp_path = config_path.with_suffix('.tmp')
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(cls._config_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, config_path)
        except (IOError, OSError):
            # 失败时清理残留临时文件，避免下次覆盖到前次半成品
            if tmp_path is not None:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
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
    def get_default_perm(cls) -> str:
        """获取【新加入连接端】的默认权限（"rw" 读写 / "ro" 只读）"""
        perm = cls.get("default_perm", "rw")
        return perm if perm in ("rw", "ro") else "rw"

    @classmethod
    def set_default_perm(cls, perm: str):
        """设置【新加入连接端】的默认权限并持久化"""
        if perm in ("rw", "ro"):
            cls.set("default_perm", perm)

    @classmethod
    def get_room_perm(cls, room_code: str) -> str:
        """获取某房间最近一次主机下发给本端的权限档位（"rw"读写 / "ro"只读）。

        连接端据此在主机离线期间保持只读不放松；无记录或记录非法时回退默认读写。
        """
        perms = cls.get("room_perm", {}) or {}
        perm = (perms or {}).get(room_code, "")
        return perm if perm in ("rw", "ro") else "rw"

    @classmethod
    def set_room_perm(cls, room_code: str, perm: str):
        """记录某房间最近一次主机下发的权限档位并持久化（主机离线时保持生效）"""
        if not room_code or perm not in ("rw", "ro"):
            return
        data = cls.load()
        perms = dict(data.get("room_perm", {}) or {})
        perms[room_code] = perm
        data["room_perm"] = perms
        cls.save()

    @classmethod
    def get_end_id(cls) -> str:
        """获取本端唯一标识（uuid4，首次访问时生成并持久化到 config.json）

        去中心化架构中作为端身份：版本向量、信号源、对端表的键。
        """
        end_id = cls.get("end_id", "")
        if not end_id:
            import uuid
            end_id = str(uuid.uuid4())
            cls.set("end_id", end_id)
        return end_id

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

    @classmethod
    def get_clean_cache_enabled(cls) -> bool:
        """获取清理缓存开关启用状态"""
        return bool(cls.get("clean_cache_enabled", False))

    @classmethod
    def set_clean_cache_enabled(cls, enabled: bool):
        """设置清理缓存开关启用状态"""
        cls.set("clean_cache_enabled", bool(enabled))

    @classmethod
    def get_confirm_leave_no_ask(cls) -> bool:
        """获取「退出且不再询问」启用状态（勾选后退出房间不再二次确认）"""
        return bool(cls.get("confirm_leave_no_ask", False))

    @classmethod
    def set_confirm_leave_no_ask(cls, enabled: bool):
        """设置「退出且不再询问」启用状态"""
        cls.set("confirm_leave_no_ask", bool(enabled))

    @classmethod
    def get_auto_check_update(cls) -> bool:
        """获取「自动检查更新」启用状态"""
        return bool(cls.get("auto_check_update", False))

    @classmethod
    def set_auto_check_update(cls, enabled: bool):
        """设置「自动检查更新」启用状态"""
        cls.set("auto_check_update", bool(enabled))

    @classmethod
    def get_receive_announcements(cls) -> bool:
        """获取「接收推送公告」启用状态"""
        return bool(cls.get("receive_announcements", True))

    @classmethod
    def set_receive_announcements(cls, enabled: bool):
        """设置「接收推送公告」启用状态"""
        cls.set("receive_announcements", bool(enabled))

    @classmethod
    def get_last_announcement(cls) -> str:
        """获取已显示公告的版本号（config.json 中的记录，空串表示从未显示）"""
        return str(cls.get("last_announcement", ""))

    @classmethod
    def set_last_announcement(cls, version: str):
        """记录已显示公告的版本号，避免下次启动重复显示"""
        cls.set("last_announcement", str(version))

    @classmethod
    def get_last_announcement_text(cls) -> str:
        """获取已接收公告的正文（config.json 中的记录，空串表示从未接收）"""
        return str(cls.get("last_announcement_text", ""))

    @classmethod
    def set_last_announcement_text(cls, text: str):
        """记录已接收公告的正文，供主界面入口启动时持久显示"""
        cls.set("last_announcement_text", str(text))

    @classmethod
    def update_reference_info(cls, exe_path: str):
        """更新参考信息到配置文件末尾（仅在 ENABLE_CHECK_UPDATE=True 时）

        Args:
            exe_path: 程序/脚本的完整路径（由调用者提供）

        参考信息包括：
        - version: 程序实时版本号
        - exe_path: 程序自身位置
        - app_name: 程序名称
        """
        if not Config.ENABLE_CHECK_UPDATE:
            return

        data = cls.load()

        # 写入参考信息
        data["version"] = Config.APP_VERSION
        data["exe_path"] = exe_path
        data["app_name"] = Config.APP_NAME

        cls.save()

    # Pro 版专属历史房间号键。与标准版共用同一 config.json 文件，但历史用独立命名空间：
    # 标准版只解析它认识的 "room_history"(room_code+ip)，天然忽略本键 → 互不污染、不读崩。
    # Pro 的历史只记房间号（去中心化加入不依赖 IP），故与标准版的 history 无法语义互用，
    # 分键隔离是最干净的方案。
    PRO_ROOM_HISTORY_KEY = "pro_room_history"

    @classmethod
    def _ensure_pro_history(cls, data: dict) -> list:
        """确保 Pro 历史命名空间存在并做一次性迁移。

        - 首次（新键缺失）时，从旧版 "room_history" 播种：取其中所有房间号并入 Pro 历史；
        - 顺带清理旧版里 Pro 此前写入的『无 IP』条目（其 IP 字段为空/缺失，已是 Pro 专属，
          标准版无法识别，留着会污染标准版列表）。

        Args:
            data: 已 load 的配置字典
        Returns:
            新键下的 Pro 历史条目列表 [{"room_code": str}, ...]
        """
        if cls.PRO_ROOM_HISTORY_KEY not in data:
            codes = []
            legacy = data.get("room_history", [])
            for h in legacy:
                c = h.get("room_code") if isinstance(h, dict) else None
                if c and c not in codes:
                    codes.append(c)
            data[cls.PRO_ROOM_HISTORY_KEY] = [{"room_code": c} for c in codes[:3]]
            # 清理旧版里无 IP 的 Pro 专属条目（保留带 IP 的，那仍属标准版可用数据）
            cleaned = [
                h for h in legacy
                if not (isinstance(h, dict) and h.get("room_code") and not h.get("ip"))
            ]
            data["room_history"] = cleaned
            # 是否实际改动了数据（新键播种或清掉了无 IP 条目）——供调用方决定是否落盘
            changed = bool(codes) or len(cleaned) != len(legacy)
            return data.get(cls.PRO_ROOM_HISTORY_KEY, []), changed
        return data.get(cls.PRO_ROOM_HISTORY_KEY, []), False

    @classmethod
    def get_room_history(cls) -> list:
        """获取 Pro 版历史房间号列表（最新在前，去重，容量 3）

        去中心化改版后不再记忆 IP，历史只保留房间号，便于新端凭编号即可入房。
        Returns:
            [room_code, ...]（最新在前）
        """
        data = cls.load()
        history, changed = cls._ensure_pro_history(data)
        if changed:
            cls.save()  # 首次迁移实际改了数据（清无 IP 条目/播种新键）时落盘，避免延迟
        codes = []
        for h in data.get(cls.PRO_ROOM_HISTORY_KEY, []):
            code = h.get("room_code") if isinstance(h, dict) else None
            if code and code not in codes:
                codes.append(code)
        return codes[:3]

    @classmethod
    def add_room_history(cls, room_code: str):
        """记录一条成功加入过/匹配过的 Pro 房间历史（上限 3 条，超出丢弃最旧）
        只记房间号，不再记 IP；写入 Pro 专属命名空间，不影响标准版 history。
        Args:
            room_code: 房间号
        """
        if not room_code:
            return
        data = cls.load()
        history, _ = cls._ensure_pro_history(data)
        # 兼容脏数据：仅保留 dict 条目，避免残留的畸形数据导致崩溃
        history = [h for h in history if isinstance(h, dict)]
        # 去重：该房间号已存在则先移除，再作为最新插入
        filtered = [h for h in history if h.get("room_code") != room_code]
        filtered.insert(0, {"room_code": room_code})
        data[cls.PRO_ROOM_HISTORY_KEY] = filtered[:3]  # 只保留最新 3 条
        cls.save()

    @classmethod
    def remove_room_history(cls, room_code: str):
        """从 Pro 版历史记录中移除指定房间号"""
        data = cls.load()
        history, _ = cls._ensure_pro_history(data)
        # 兼容脏数据：仅保留 dict 条目
        history = [h for h in history if isinstance(h, dict)]
        data[cls.PRO_ROOM_HISTORY_KEY] = [h for h in history if h.get("room_code") != room_code]
        cls.save()