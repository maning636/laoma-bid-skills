"""规则引擎 —— 规则包读取、过滤、override 合并。

纯函数模块，无状态；不做 IO 调度以外的任何事。
入口：``load_rules`` / ``filter_rules`` / ``effective_rules``。
"""

from rules_engine.registry import effective_rules, filter_rules, load_rules

__all__ = ["effective_rules", "filter_rules", "load_rules"]
