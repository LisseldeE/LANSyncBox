"""
国际化支持模块
支持中英文切换
"""
from typing import Dict


class I18n:
    """国际化管理器"""
    
    # 当前语言
    _current_lang = "zh_CN"
    
    # 翻译字典
    _translations: Dict[str, Dict[str, str]] = {
        "zh_CN": {
            # 应用名称
            "app_name": "LANSyncBox",
            "app_title": "局域网文件同步工具",
            
            # 主界面
            "create_room": "创建房间",
            "join_room": "加入房间",
            "about": "关于",
            "settings": "设置",
            "language": "语言",
            "chinese": "中文",
            "english": "English",
            "manage_cache": "管理缓存",
            "manage_cache_error": "无法打开缓存文件夹",
            "manage_cache_not_found": "缓存文件夹不存在",
            
            # 创建房间对话框
            "create_room_title": "创建房间",
            "room_code": "房间号",
            "room_code_hint": "6位数字房间号",
            "regenerate_room_code": "重新生成",
            "fixed_room_code": "固定房间号",
            "fixed_room_code_hint": "启用后固定房间号，不自动刷新",
            "password": "密码",
            "password_hint": "可选，留空则无需密码",
            "sync_folder": "同步文件夹",
            "check_availability": "检测可用性",
            "room_code_available": "房间号可用",
            "room_code_unavailable": "房间号已被占用",
            "create": "创建",
            "cancel": "取消",
            "close": "关闭",
            
            # 加入房间对话框
            "join_room_title": "加入房间",
            "room_code_required": "请输入房间号",
            "password_required": "此房间需要密码",
            "connect": "连接",
            "connecting": "正在连接...",
            "connection_failed": "连接失败",
            "invalid_room_code": "无效的房间号",
            "incorrect_password": "密码错误",
            "host_address_optional": "主机地址 (可选)",
            "host_address_hint": "例如: 192.168.1.100 (留空则自动发现)",
            "searching": "搜索中...",
            "searching_room": "正在搜索房间...",
            "room_not_found": "未找到该房间，请检查房间号是否正确",
            "room_found": "已找到房间 ({ip})",
            "room_found_manual": "已找到房间",
            "ready_waiting": "就绪，等待输入...",
            "version_mismatch": "版本不一致（本机 {local} / 主机 {remote}），请升级后连接",
            "version_match": "版本一致，可连接",
            
            # 同步界面
            "host_mode": "主机端",
            "client_mode": "连接端",
            "room_info": "房间: {code}",
            "sync_folder_path": "同步文件夹: {path}",
            "disconnect": "断开连接",
            "leave_room": "离开房间",
            "transfer_log": "传输日志",
            
            # 文件列表
            "file_name": "文件名",
            "file_size": "大小",
            "file_modified": "修改时间",
            "file_status": "状态",
            "drag_files_hint": "拖拽文件以操作",
            "folder": "文件夹",
            "file": "文件",
            
            # 文件状态
            "status_synced": "已同步",
            "status_syncing": "正在同步",
            "status_conflict": "冲突",
            "status_failed": "同步失败",
            "status_disconnected": "已断开连接",
            
            # 右键菜单
            "copy": "复制",
            "cut": "剪切",
            "paste": "粘贴",
            "delete": "删除",
            "rename": "重命名",
            "select_all": "全选",
            "refresh": "刷新",
            "go_up": "↑ 上级",
            
            # 拖拽操作
            "drag_add": "添加文件",
            "drag_copy": "复制文件",
            "drag_move": "移动文件",
            "drag_files_count": "{count} 个文件",
            
            # 确认对话框
            "confirm_delete": "确认删除",
            "confirm_delete_msg": "确定要删除选中的文件吗？",
            "confirm_replace": "确认替换",
            "confirm_replace_msg": "文件已存在，是否替换？",
            "confirm_leave": "确认离开",
            "confirm_leave_msg": "离开房间后，同步将停止。是否继续？",
            "yes": "是",
            "no": "否",
            "ok": "确定",
            
            # 验证相关
            "auth_failed": "验证失败",
            "auth_failed_msg": "验证失败: {msg}",
            "disconnected": "断开连接",
            
            # 错误信息
            "error_file_exists": "文件已存在",
            "error_file_not_found": "文件不存在",
            "error_permission_denied": "权限不足",
            "error_file_in_use": "文件正在使用中",
            "error_invalid_name": "无效的文件名",
            "error_name_too_long": "文件名过长",
            "error_forbidden_char": "文件名包含非法字符",
            
            # 提示信息
            "tip_open_file": "请拖拽保存至其他位置再打开",
            "tip_first_use": "所有文件操作请在程序内完成",
            "tip_external_modify": "外部修改不会自动同步",
            
            # 传输日志
            "log_upload": "上传",
            "log_download": "下载",
            "log_delete": "删除",
            "log_rename": "重命名",
            "log_success": "成功",
            "log_failed": "失败",
            
            # 进度对话框
            "copying_files": "正在复制文件",
            "syncing_files": "正在同步文件",
            "progress": "进度",
            "speed": "速度",
            "remaining_time": "剩余时间",
            
            # 同步记录
            "log_time": "时间",
            "log_file": "文件",
            "log_action": "操作",
            "log_info": "信息",
            "log_progress": "进度",
            "online_count": "在线",
            
            # 关于对话框
            "about_title": "关于 LANSyncBox",
            "about_version": "版本: {version}",
            "about_version_label": "版本",
            "about_author": "作者",
            "about_description": "局域网文件实时同步工具\n实现多人隔空文件共享",
            "about_check_update": "检查更新",
            "about_info": "提示",
            "about_email_copied": "邮箱已复制到剪贴板",
            "about_no_tags": "未找到版本信息",
            "about_parse_error": "无法解析当前版本号",
            "about_remote_parse_error": "无法解析远程版本号",
            "about_new_version": "发现新版本 {version}，是否前往下载？",
            "about_latest": "已是最新版本",
            "about_yes": "是",
            "about_no": "否",
            "about_network_error": "网络连接失败：{error}",
            "about_check_failed": "检查失败：{error}",
        },
        "en_US": {
            # App name
            "app_name": "LANSyncBox",
            "app_title": "LAN File Sync Tool",
            
            # Main window
            "create_room": "Create Room",
            "join_room": "Join Room",
            "about": "About",
            "settings": "Settings",
            "language": "Language",
            "chinese": "中文",
            "english": "English",
            "manage_cache": "Manage Cache",
            "manage_cache_error": "Cannot open cache folder",
            "manage_cache_not_found": "Cache folder does not exist",
            
            # Create room dialog
            "create_room_title": "Create Room",
            "room_code": "Room Code",
            "room_code_hint": "6-digit room code",
            "regenerate_room_code": "Regenerate",
            "fixed_room_code": "Fixed Room Code",
            "fixed_room_code_hint": "When enabled, the room code is fixed and will not auto-refresh",
            "password": "Password",
            "password_hint": "Optional, leave empty for no password",
            "sync_folder": "Sync Folder",
            "check_availability": "Check Availability",
            "room_code_available": "Room code available",
            "room_code_unavailable": "Room code already in use",
            "create": "Create",
            "cancel": "Cancel",
            "close": "Close",
            
            # Join room dialog
            "join_room_title": "Join Room",
            "room_code_required": "Please enter room code",
            "password_required": "This room requires password",
            "connect": "Connect",
            "connecting": "Connecting...",
            "connection_failed": "Connection failed",
            "invalid_room_code": "Invalid room code",
            "incorrect_password": "Incorrect password",
            "host_address_optional": "Host Address (Optional)",
            "host_address_hint": "e.g. 192.168.1.100 (Leave empty for auto-discovery)",
            "searching": "Searching...",
            "searching_room": "Searching for room...",
            "room_not_found": "Room not found, please check the room code",
            "room_found": "Room found ({ip})",
            "room_found_manual": "Room found",
            "ready_waiting": "Ready, waiting for input...",
            "version_mismatch": "Version mismatch (local {local} / host {remote}), please upgrade before connecting",
            "version_match": "Version matches, ready to connect",
            
            # Sync window
            "host_mode": "Host",
            "client_mode": "Client",
            "room_info": "Room: {code}",
            "sync_folder_path": "Sync folder: {path}",
            "disconnect": "Disconnect",
            "leave_room": "Leave Room",
            "transfer_log": "Transfer Log",
            
            # File list
            "file_name": "Name",
            "file_size": "Size",
            "file_modified": "Modified",
            "file_status": "Status",
            "drag_files_hint": "Drag files to operate",
            "folder": "Folder",
            "file": "File",
            
            # File status
            "status_synced": "Synced",
            "status_syncing": "Syncing",
            "status_conflict": "Conflict",
            "status_failed": "Failed",
            "status_disconnected": "Disconnected",
            
            # Context menu
            "copy": "Copy",
            "cut": "Cut",
            "paste": "Paste",
            "delete": "Delete",
            "rename": "Rename",
            "select_all": "Select All",
            "refresh": "Refresh",
            "go_up": "↑ Up",
            
            # Drag operations
            "drag_add": "Add Files",
            "drag_copy": "Copy Files",
            "drag_move": "Move Files",
            "drag_files_count": "{count} file(s)",
            
            # Confirm dialogs
            "confirm_delete": "Confirm Delete",
            "confirm_delete_msg": "Are you sure you want to delete selected files?",
            "confirm_replace": "Confirm Replace",
            "confirm_replace_msg": "File already exists. Replace it?",
            "confirm_leave": "Confirm Leave",
            "confirm_leave_msg": "Sync will stop after leaving the room. Continue?",
            "yes": "Yes",
            "no": "No",
            "ok": "OK",
            
            # Authentication
            "auth_failed": "Authentication Failed",
            "auth_failed_msg": "Authentication failed: {msg}",
            "disconnected": "Disconnected",
            
            # Error messages
            "error_file_exists": "File already exists",
            "error_file_not_found": "File not found",
            "error_permission_denied": "Permission denied",
            "error_file_in_use": "File is in use",
            "error_invalid_name": "Invalid file name",
            "error_name_too_long": "File name too long",
            "error_forbidden_char": "File name contains forbidden characters",
            
            # Tips
            "tip_open_file": "Please drag to save elsewhere before opening",
            "tip_first_use": "All file operations should be done within the program",
            "tip_external_modify": "External modifications will not sync automatically",
            
            # Transfer log
            "log_upload": "Upload",
            "log_download": "Download",
            "log_delete": "Delete",
            "log_rename": "Rename",
            "log_success": "Success",
            "log_failed": "Failed",
            
            # Progress dialog
            "copying_files": "Copying Files",
            "syncing_files": "Syncing Files",
            "progress": "Progress",
            "speed": "Speed",
            "remaining_time": "Remaining",
            
            # Sync records
            "log_time": "Time",
            "log_file": "File",
            "log_action": "Action",
            "log_info": "Info",
            "log_progress": "Progress",
            "online_count": "Online",
            
            # About dialog
            "about_title": "About LANSyncBox",
            "about_version": "Version: {version}",
            "about_version_label": "Version",
            "about_author": "Author",
            "about_description": "LAN Real-time File Sync Tool\nMulti-user File Sharing",
            "about_check_update": "Check for Updates",
            "about_info": "Info",
            "about_email_copied": "Email copied to clipboard",
            "about_no_tags": "No version tags found",
            "about_parse_error": "Cannot parse current version",
            "about_remote_parse_error": "Cannot parse remote version",
            "about_new_version": "New version {version} found. Download?",
            "about_latest": "Already the latest version",
            "about_yes": "Yes",
            "about_no": "No",
            "about_network_error": "Network error: {error}",
            "about_check_failed": "Check failed: {error}",
        }
    }
    
    @classmethod
    def set_language(cls, lang: str):
        """设置语言"""
        if lang in cls._translations:
            cls._current_lang = lang
    
    @classmethod
    def get_language(cls) -> str:
        """获取当前语言"""
        return cls._current_lang
    
    @classmethod
    def tr(cls, key: str, **kwargs) -> str:
        """翻译文本"""
        lang_dict = cls._translations.get(cls._current_lang, cls._translations["zh_CN"])
        text = lang_dict.get(key, key)
        
        # 格式化参数
        if kwargs:
            try:
                text = text.format(**kwargs)
            except (KeyError, ValueError):
                pass
        
        return text
    
    @classmethod
    def get_available_languages(cls) -> list:
        """获取可用语言列表"""
        return list(cls._translations.keys())