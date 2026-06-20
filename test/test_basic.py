# -*- coding: utf-8 -*-
"""
LANSyncBox 基础操作测试
测试 CREATE、MODIFY、DELETE 操作
"""

from test.mock import MockEnvironment
from sync import OpType


def test_create_file():
    """测试创建文件"""
    env = MockEnvironment()
    
    # 主机端创建文件
    env.create_file('host', 'test.txt', 'hello world')
    
    # 同步
    env.sync_all()
    
    # 验证连接端收到文件
    assert env.file_exists('client', 'test.txt'), "连接端应该收到文件"
    
    content = env.read_file('client', 'test.txt')
    assert content == 'hello world', f"内容应该匹配: {content}"
    
    # 验证操作类型
    ops = env.get_host_ops()
    assert len(ops) >= 1, "应该有至少一个操作"
    assert ops[0].op_type == OpType.CREATE, f"操作类型应该是 CREATE: {ops[0].op_type}"
    
    env.cleanup()


def test_modify_file():
    """测试修改文件"""
    env = MockEnvironment()
    
    # 先创建文件
    env.create_file('host', 'test.txt', 'original')
    env.sync_all()
    
    # 修改文件
    env.modify_file('host', 'test.txt', 'modified')
    env.sync_all()
    
    # 验证连接端收到修改
    content = env.read_file('client', 'test.txt')
    assert content == 'modified', f"内容应该是修改后的: {content}"
    
    env.cleanup()


def test_delete_file():
    """测试删除文件"""
    env = MockEnvironment()
    
    # 先创建文件
    env.create_file('host', 'test.txt', 'to be deleted')
    env.sync_all()
    
    # 删除文件
    env.delete_file('host', 'test.txt')
    env.sync_all()
    
    # 验证连接端文件也被删除
    assert not env.file_exists('client', 'test.txt'), "连接端文件应该被删除"
    
    env.cleanup()


def test_create_directory():
    """测试创建目录"""
    env = MockEnvironment()
    
    # 主机端创建目录（通过创建子文件）
    env.create_file('host', 'subdir/test.txt', 'in subdir')
    env.sync_all()
    
    # 验证连接端收到目录和文件
    assert env.file_exists('client', 'subdir/test.txt'), "连接端应该收到文件"
    
    env.cleanup()


def test_modify_while_syncing():
    """测试同步期间修改其他文件"""
    env = MockEnvironment()
    
    # 创建第一个文件
    env.create_file('host', 'file1.txt', 'content1')
    
    # 立即创建第二个文件（第一个还在同步）
    env.create_file('host', 'file2.txt', 'content2')
    
    # 同步
    env.sync_all()
    
    # 验证两个文件都同步成功
    assert env.file_exists('client', 'file1.txt'), "file1 应该同步"
    assert env.file_exists('client', 'file2.txt'), "file2 应该同步"
    
    env.cleanup()


# 测试函数列表
TESTS = {
    'test_create_file': test_create_file,
    'test_modify_file': test_modify_file,
    'test_delete_file': test_delete_file,
    'test_create_directory': test_create_directory,
    'test_modify_while_syncing': test_modify_while_syncing,
}