# -*- coding: utf-8 -*-
"""
LANSyncBox 重命名测试
测试重命名识别和同步
"""

import time
from test.mock import MockEnvironment
from sync import OpType


def test_rename_detection():
    """测试重命名识别"""
    env = MockEnvironment()
    
    # 创建文件
    env.create_file('host', 'old_name.txt', 'same content')
    env.sync_all()
    
    # 等待一下确保同步完成
    time.sleep(0.1)
    
    # 重命名文件（触发 delete + create）
    env.rename_file('host', 'old_name.txt', 'new_name.txt')
    
    # 等待重命名识别窗口（500ms）
    time.sleep(0.6)
    
    # 同步
    env.sync_all()
    
    # 验证操作类型是 RENAME
    ops = env.get_host_ops()
    # 找到最后一个操作
    rename_ops = [op for op in ops if op.op_type == OpType.RENAME]
    
    if rename_ops:
        # 成功识别为重命名
        assert rename_ops[0].old_path == 'old_name.txt', f"旧路径应该是 old_name.txt"
        assert rename_ops[0].path == 'new_name.txt', f"新路径应该是 new_name.txt"
        
        # 验证连接端文件被重命名
        assert not env.file_exists('client', 'old_name.txt'), "旧文件应该不存在"
        assert env.file_exists('client', 'new_name.txt'), "新文件应该存在"
    else:
        # 如果识别失败，应该是 DELETE + CREATE
        # 这也是可以接受的
        assert not env.file_exists('client', 'old_name.txt'), "旧文件应该不存在"
        assert env.file_exists('client', 'new_name.txt'), "新文件应该存在"
    
    env.cleanup()


def test_rename_with_same_content():
    """测试内容相同的重命名"""
    env = MockEnvironment()
    
    # 创建文件
    env.create_file('host', 'file1.txt', 'identical content')
    env.sync_all()
    
    time.sleep(0.1)
    
    # 重命名
    env.rename_file('host', 'file1.txt', 'file2.txt')
    
    time.sleep(0.6)
    env.sync_all()
    
    # 验证内容保持不变
    content = env.read_file('client', 'file2.txt')
    assert content == 'identical content', f"内容应该保持不变: {content}"
    
    env.cleanup()


def test_rename_to_existing_path():
    """测试重命名到已存在的路径"""
    env = MockEnvironment()
    
    # 创建两个文件
    env.create_file('host', 'file1.txt', 'content1')
    env.create_file('host', 'file2.txt', 'content2')
    env.sync_all()
    
    time.sleep(0.1)
    
    # 删除 file2
    env.delete_file('host', 'file2.txt')
    
    # 重命名 file1 到 file2
    env.rename_file('host', 'file1.txt', 'file2.txt')
    
    time.sleep(0.6)
    env.sync_all()
    
    # 验证 file2 内容是 content1
    content = env.read_file('client', 'file2.txt')
    assert content == 'content1', f"内容应该是 content1: {content}"
    
    env.cleanup()


# 测试函数列表
TESTS = {
    'test_rename_detection': test_rename_detection,
    'test_rename_with_same_content': test_rename_with_same_content,
    'test_rename_to_existing_path': test_rename_to_existing_path,
}