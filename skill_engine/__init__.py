"""Skill 引擎 —— 技能发现、加载与调度的对外门面。

对外导出的 5 个构件：

- ``Skill``         ：技能抽象基类（子类需要实现 ``run`` 与 ``schema``）；
- ``SkillContext``  ：执行上下文数据类，携带调用方元信息与请求载荷；
- ``SkillResult``   ：执行结果数据类，统一承载 status / payload / error；
- ``SkillRegistry`` ：进程内技能注册表（默认单例，可注入测试实例）；
- ``SkillLoader``   ：从磁盘发现并加载技能包（扫描技能目录）。

顶层用 ``__getattr__`` 延迟导入：``loader.py`` 会遍历技能目录并拉起子模块
加载逻辑，``registry.py`` 维护进程内状态；如果仅使用 ``Skill`` /
``SkillContext`` / ``SkillResult`` 这组数据契约，就不需要为这两个重模块
付出加载代价。
"""

from __future__ import annotations

__all__ = ["Skill", "SkillContext", "SkillLoader", "SkillRegistry", "SkillResult"]

# 名字 -> 所在子模块。前三个数据契约集中在 base.py，其余按职责一一对应。
_LAZY_EXPORTS: dict[str, str] = {
    "Skill": "skill_engine.base",
    "SkillContext": "skill_engine.base",
    "SkillResult": "skill_engine.base",
    "SkillRegistry": "skill_engine.registry",
    "SkillLoader": "skill_engine.loader",
}


def __getattr__(name: str):
    """按需导入并返回导出符号；未知名字抛 AttributeError。"""
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__():
    """列出模块对外可见的名字（PEP 562 要求：定义了 __getattr__ 就该定义 __dir__）。"""
    return sorted(__all__)
