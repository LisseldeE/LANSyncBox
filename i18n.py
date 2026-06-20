# -*- coding: utf-8 -*-
"""
LANSyncBox 国际化模块
支持中英文切换
"""

class I18n:
    """国际化管理类"""
    
    # 当前语言
    _current_lang = 'zh'
    
    # 翻译字典
    _translations = {
        'zh': {
            # 主窗口
            'main_title': 'LANSyncBox',
            'main_create': '创建连接',
            'main_join': '加入连接',
            'main_about': '关于',
            'main_exit': '退出',
            
            # 创建房间对话框
            'create_title': '创建连接',
            'create_room_label': '房间号（6位数字）：',
            'create_room_placeholder': '请输入6位数字房间号',
            'create_password_label': '密码（可选）：',
            'create_password_placeholder': '不设置密码则无需验证',
            'create_folder_label': '同步文件夹：',
            'create_folder_select': '选择文件夹',
            'create_peer_sync': '允许连接端互相同步',
            'create_btn': '创建',
            'create_cancel': '取消',
            'create_room_exists': '房间号已存在',
            'create_room_invalid': '房间号必须是6位数字',
            'create_folder_required': '请选择同步文件夹',
            'create_success': '房间创建成功',
            
            # 加入房间对话框
            'join_title': '加入连接',
            'join_room_label': '房间号：',
            'join_room_placeholder': '请输入房间号',
            'join_password_label': '密码：',
            'join_password_placeholder': '如有密码请输入',
            'join_folder_label': '同步文件夹：',
            'join_folder_select': '选择文件夹',
            'join_btn': '连接',
            'join_cancel': '取消',
            'join_room_required': '请输入房间号',
            'join_folder_required': '请选择同步文件夹',
            'join_password_required': '请输入密码',
            'join_auth_failed': '密码验证失败',
            'join_connect_failed': '连接失败',
            'join_success': '连接成功',
            
            # 同步窗口
            'sync_title': '同步状态',
            'sync_room': '房间号：',
            'sync_role_host': '主机端',
            'sync_role_client': '连接端',
            'sync_folder': '同步文件夹：',
            'sync_clients': '连接端列表',
            'sync_files': '同步记录',
            'sync_hide': '对外隐藏本机文件',
            'sync_show': '显示本机文件',
            'sync_exit': '退出',
            'sync_client_disconnected': '已断开连接',
            'sync_host_disconnected': '房间已关闭',
            'sync_copy_room': '点击复制房间号',
            
            # 关于对话框
            'about_title': '关于',
            'about_version': '版本',
            'about_desc': '局域网文件实时同步工具，实现多人隔空文件共享',
            'about_author': '作者',
            'about_check_update': '检查更新',
            'about_close': '关闭',
            'about_email_copied': '邮箱已复制到剪贴板',
            'about_no_tags': '未找到版本标签',
            'about_parse_error': '无法解析当前版本号',
            'about_remote_parse_error': '无法解析远程版本号',
            'about_new_version': '发现新版本 {0}！\n是否前往下载？',
            'about_latest': '当前已是最新版本',
            'about_yes': '是',
            'about_no': '否',
            'about_network_error': '网络错误：{0}\n请检查网络连接',
            'about_check_failed': '检查更新失败：{0}',
            
            # 通用
            'common_select_folder': '选择文件夹',
            'common_copy': '复制',
            'common_delete': '删除',
            'common_file': '文件',
            'common_folder': '文件夹',
            'common_create': '创建',
            'common_modify': '修改',
            'common_sync': '同步',
            'common_receive': '接收',
            'common_send': '发送',
            'common_from': '来自',
            'common_to': '发送至',
            'common_progress': '进度',
            'common_complete': '完成',
            'common_error': '错误',
            'common_warning': '警告',
            'common_info': '提示',
            'common_check': '检测',
            'common_options': '选项',
            'common_searching': '正在搜索...',
            'common_found': '已找到',
            'common_not_found': '未找到',
            'common_room_mismatch': '房间号不匹配',
            'common_connecting': '连接中...',
            'common_connected': '已连接',
            'common_disconnected': '已断开',
            'common_online': '在线',
            'common_mode': '模式',
            'common_status': '状态',
            'common_connect': '连接',
            
            # 创建房间额外
            'create_failed': '创建房间失败，请重试',
            'create_room_available': '房间号 {0} 可用',
            'create_room_occupied': '房间号 {0} 已被占用',
            
            # 加入房间额外
            'join_searching': '正在搜索主机...',
            'join_found_host': '已找到主机: {0}',
            'join_search_timeout': '搜索超时',
            'join_auto_search': '输入房间号后自动搜索',
            'join_folder_not_exists': '文件夹不存在',
            'join_room_6digit': '请输入6位数字房间号',
            
            # 同步窗口额外
            'sync_hide_files': '隐藏本机文件',
            'sync_full_sync': '全量同步',
            'sync_peer_sync': '互相同步',
            'sync_peer_on': '开',
            'sync_peer_off': '关',
            'sync_records': '同步记录',
            'sync_clients_list': '连接端列表',
            'sync_file_name': '文件名',
            'sync_action': '操作',
            'sync_time': '时间',
            'sync_source': '来源',
            'sync_client_ip': 'IP地址',
            'sync_client_hide': '隐藏',
            'sync_progress': '进度',
        },
        'en': {
            # Main window
            'main_title': 'LANSyncBox',
            'main_create': 'Create Connection',
            'main_join': 'Join Connection',
            'main_about': 'About',
            'main_exit': 'Exit',
            
            # Create room dialog
            'create_title': 'Create Connection',
            'create_room_label': 'Room Code (6 digits):',
            'create_room_placeholder': 'Enter 6-digit room code',
            'create_password_label': 'Password (optional):',
            'create_password_placeholder': 'No password = no verification',
            'create_folder_label': 'Sync Folder:',
            'create_folder_select': 'Select Folder',
            'create_peer_sync': 'Allow peer-to-peer sync',
            'create_btn': 'Create',
            'create_cancel': 'Cancel',
            'create_room_exists': 'Room code already exists',
            'create_room_invalid': 'Room code must be 6 digits',
            'create_folder_required': 'Please select sync folder',
            'create_success': 'Room created successfully',
            
            # Join room dialog
            'join_title': 'Join Connection',
            'join_room_label': 'Room Code:',
            'join_room_placeholder': 'Enter room code',
            'join_password_label': 'Password:',
            'join_password_placeholder': 'Enter password if required',
            'join_folder_label': 'Sync Folder:',
            'join_folder_select': 'Select Folder',
            'join_btn': 'Connect',
            'join_cancel': 'Cancel',
            'join_room_required': 'Please enter room code',
            'join_folder_required': 'Please select sync folder',
            'join_password_required': 'Please enter password',
            'join_auth_failed': 'Password verification failed',
            'join_connect_failed': 'Connection failed',
            'join_success': 'Connected successfully',
            
            # Sync window
            'sync_title': 'Sync Status',
            'sync_room': 'Room Code:',
            'sync_role_host': 'Host',
            'sync_role_client': 'Client',
            'sync_folder': 'Sync Folder:',
            'sync_clients': 'Connected Clients',
            'sync_files': 'Sync Records',
            'sync_hide': 'Hide files from others',
            'sync_show': 'Show files to others',
            'sync_exit': 'Exit',
            'sync_client_disconnected': 'Disconnected',
            'sync_host_disconnected': 'Room closed',
            'sync_copy_room': 'Click to copy room code',
            
            # About dialog
            'about_title': 'About',
            'about_version': 'Version',
            'about_desc': 'LAN real-time file sync tool for multi-user wireless file sharing',
            'about_author': 'Author',
            'about_check_update': 'Check Update',
            'about_close': 'Close',
            'about_email_copied': 'Email copied to clipboard',
            'about_no_tags': 'No version tags found',
            'about_parse_error': 'Cannot parse current version',
            'about_remote_parse_error': 'Cannot parse remote version',
            'about_new_version': 'New version {0} found!\nDownload now?',
            'about_latest': 'Already the latest version',
            'about_yes': 'Yes',
            'about_no': 'No',
            'about_network_error': 'Network error: {0}\nPlease check connection',
            'about_check_failed': 'Update check failed: {0}',
            
            # Common
            'common_select_folder': 'Select Folder',
            'common_copy': 'Copy',
            'common_delete': 'Delete',
            'common_file': 'File',
            'common_folder': 'Folder',
            'common_create': 'Create',
            'common_modify': 'Modify',
            'common_sync': 'Sync',
            'common_receive': 'Receive',
            'common_send': 'Send',
            'common_from': 'From',
            'common_to': 'To',
            'common_progress': 'Progress',
            'common_complete': 'Complete',
            'common_error': 'Error',
            'common_warning': 'Warning',
            'common_info': 'Info',
            'common_check': 'Check',
            'common_options': 'Options',
            'common_searching': 'Searching...',
            'common_found': 'Found',
            'common_not_found': 'Not found',
            'common_room_mismatch': 'Room code mismatch',
            'common_connecting': 'Connecting...',
            'common_connected': 'Connected',
            'common_disconnected': 'Disconnected',
            'common_online': 'Online',
            'common_mode': 'Mode',
            'common_status': 'Status',
            'common_connect': 'Connect',
            
            # Create room extra
            'create_failed': 'Failed to create room, please retry',
            'create_room_available': 'Room {0} available',
            'create_room_occupied': 'Room {0} already occupied',
            
            # Join room extra
            'join_searching': 'Searching for host...',
            'join_found_host': 'Host found: {0}',
            'join_search_timeout': 'Search timeout',
            'join_auto_search': 'Auto search after entering room code',
            'join_folder_not_exists': 'Folder does not exist',
            'join_room_6digit': 'Please enter 6-digit room code',
            
            # Sync window extra
            'sync_hide_files': 'Hide local files',
            'sync_full_sync': 'Full Sync',
            'sync_peer_sync': 'Peer sync',
            'sync_peer_on': 'On',
            'sync_peer_off': 'Off',
            'sync_records': 'Sync Records',
            'sync_clients_list': 'Connected Clients',
            'sync_file_name': 'File Name',
            'sync_action': 'Action',
            'sync_time': 'Time',
            'sync_source': 'Source',
            'sync_client_ip': 'IP Address',
            'sync_client_hide': 'Hidden',
            'sync_progress': 'Progress',
        }
    }
    
    @classmethod
    def set_lang(cls, lang):
        """设置当前语言"""
        if lang in cls._translations:
            cls._current_lang = lang
    
    @classmethod
    def get_lang(cls):
        """获取当前语言"""
        return cls._current_lang
    
    @classmethod
    def t(cls, key, *args):
        """获取翻译文本"""
        text = cls._translations.get(cls._current_lang, {}).get(key, key)
        if args:
            text = text.format(*args)
        return text
    
    @classmethod
    def get_all_keys(cls):
        """获取所有翻译键"""
        return list(cls._translations['zh'].keys())