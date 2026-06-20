# -*- coding: utf-8 -*-
"""
LANSyncBox 测试运行器
执行测试用例并生成报告
"""

import sys
import time
from typing import List, Dict
from dataclasses import dataclass


@dataclass
class TestResult:
    """测试结果"""
    name: str
    passed: bool
    message: str
    duration: float


class TestRunner:
    """测试运行器"""
    
    def __init__(self):
        self._results: List[TestResult] = []
    
    def run_test(self, name: str, test_func) -> TestResult:
        """
        运行单个测试
        Args:
            name: 测试名称
            test_func: 测试函数
        Returns:
            测试结果
        """
        start_time = time.time()
        
        try:
            test_func()
            result = TestResult(
                name=name,
                passed=True,
                message="OK",
                duration=time.time() - start_time
            )
        except AssertionError as e:
            result = TestResult(
                name=name,
                passed=False,
                message=str(e),
                duration=time.time() - start_time
            )
        except Exception as e:
            result = TestResult(
                name=name,
                passed=False,
                message=f"Error: {e}",
                duration=time.time() - start_time
            )
        
        self._results.append(result)
        return result
    
    def run_all(self, tests: Dict[str, callable]):
        """
        运行所有测试
        Args:
            tests: {name: test_func}
        """
        for name, test_func in tests.items():
            self.run_test(name, test_func)
    
    def get_results(self) -> List[TestResult]:
        """获取所有测试结果"""
        return self._results.copy()
    
    def get_passed_count(self) -> int:
        """获取通过的测试数量"""
        return sum(1 for r in self._results if r.passed)
    
    def get_failed_count(self) -> int:
        """获取失败的测试数量"""
        return sum(1 for r in self._results if not r.passed)
    
    def print_report(self):
        """打印测试报告"""
        print("\n" + "=" * 60)
        print("测试报告")
        print("=" * 60)
        
        for result in self._results:
            status = "[PASS]" if result.passed else "[FAIL]"
            print(f"{status} | {result.name} | {result.message} | {result.duration:.3f}s")
        
        print("=" * 60)
        passed = self.get_passed_count()
        failed = self.get_failed_count()
        total = len(self._results)
        
        print(f"总计: {total} | 通过: {passed} | 失败: {failed}")
        print("=" * 60 + "\n")
        
        return failed == 0
    
    def clear(self):
        """清除测试结果"""
        self._results.clear()