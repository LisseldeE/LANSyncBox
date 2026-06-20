"""
测试脚本
验证各个模块是否可以正常导入和运行
"""
import sys
from pathlib import Path

print("开始测试...")

# 测试配置模块
try:
    from config import Config
    print("[OK] 配置模块导入成功")
    print(f"  - 应用名称: {Config.APP_NAME}")
    print(f"  - 应用版本: {Config.APP_VERSION}")
    print(f"  - 同步文件夹: {Config.get_sync_folder()}")
except Exception as e:
    print(f"[FAIL] 配置模块导入失败: {e}")
    sys.exit(1)

# 测试国际化模块
try:
    from i18n import I18n
    print("[OK] 国际化模块导入成功")
    print(f"  - 当前语言: {I18n.get_language()}")
    print(f"  - 应用名称: {I18n.tr('app_name')}")
    print(f"  - 创建房间: {I18n.tr('create_room')}")
    
    # 测试切换语言
    I18n.set_language("en_US")
    print(f"  - 切换到英文: {I18n.tr('create_room')}")
    I18n.set_language("zh_CN")
except Exception as e:
    print(f"[FAIL] 国际化模块导入失败: {e}")
    sys.exit(1)

# 测试文件管理器
try:
    from sync.file_manager import FileManager
    print("[OK] 文件管理器导入成功")
    
    # 创建测试文件夹
    test_folder = Config.get_sync_folder() / "test_123456"
    test_folder.mkdir(exist_ok=True)
    
    # 创建文件管理器
    file_manager = FileManager(test_folder)
    print(f"  - 文件管理器创建成功: {file_manager.folder_path}")
    
    # 创建测试文件
    test_file = test_folder / "test.txt"
    test_file.write_text("Hello, LANSyncBox!")
    print(f"  - 创建测试文件: {test_file}")
    
    # 获取文件列表
    files = file_manager.get_file_list()
    print(f"  - 文件列表: {len(files)} 个文件")
    for f in files:
        print(f"    - {f['name']} ({f['size']} bytes)")
    
    # 计算文件哈希
    hash_value = file_manager.calculate_file_hash(test_file)
    print(f"  - 文件哈希: {hash_value}")
    
    # 清理测试文件
    test_file.unlink()
    test_folder.rmdir()
    print("  - 清理测试文件完成")
except Exception as e:
    print(f"[FAIL] 文件管理器测试失败: {e}")
    sys.exit(1)

# 测试网络协议
try:
    from network.protocol import SyncProtocol, SyncMessage, MessageType
    print("[OK] 网络协议导入成功")
    
    # 创建消息
    connect_msg = SyncProtocol.create_connect_message("123456", "")
    print(f"  - 创建连接消息: {connect_msg.type}")
    print(f"  - 消息JSON: {connect_msg.to_json()[:100]}...")
    
    # 解析消息
    parsed_msg = SyncMessage.from_json(connect_msg.to_json())
    print(f"  - 解析消息成功: {parsed_msg.type}")
except Exception as e:
    print(f"[FAIL] 网络协议测试失败: {e}")
    sys.exit(1)

# 测试同步引擎
try:
    from sync.sync_engine import SyncEngine
    print("[OK] 同步引擎导入成功")
    
    # 创建同步引擎
    test_folder = Config.get_sync_folder() / "test_engine"
    test_folder.mkdir(exist_ok=True)
    file_manager = FileManager(test_folder)
    sync_engine = SyncEngine(file_manager)
    print(f"  - 同步引擎创建成功")
    
    # 添加操作
    sync_engine.add_operation({
        'type': 'test',
        'file_path': 'test.txt'
    })
    print(f"  - 添加测试操作成功")
    
    # 清理
    test_folder.rmdir()
except Exception as e:
    print(f"[FAIL] 同步引擎测试失败: {e}")
    sys.exit(1)

print("\n所有测试通过！")
print("第一阶段基础功能已完成。")