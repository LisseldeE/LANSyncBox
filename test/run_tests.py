# -*- coding: utf-8 -*-
"""
LANSyncBox 测试入口
运行所有测试
"""

import sys
import os

# 确保可以导入模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt5.QtWidgets import QApplication
from test.runner import TestRunner
from test.test_basic import TESTS as BASIC_TESTS
from test.test_rename import TESTS as RENAME_TESTS
from test.test_conflict import TESTS as CONFLICT_TESTS


def run_all_tests():
    """运行所有测试"""
    # 创建 QApplication（某些模块需要）
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)
    
    runner = TestRunner()
    
    print("\n开始运行测试...\n")
    
    # 运行基础测试
    print("=== 基础操作测试 ===")
    runner.run_all(BASIC_TESTS)
    
    # 运行重命名测试
    print("\n=== 重命名测试 ===")
    runner.run_all(RENAME_TESTS)
    
    # 运行冲突测试
    print("\n=== 冲突测试 ===")
    runner.run_all(CONFLICT_TESTS)
    
    # 打印报告
    success = runner.print_report()
    
    return success


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)