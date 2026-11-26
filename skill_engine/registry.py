"""Skill 引擎 — 注册表。

按名称管理已加载的 ``Skill`` 子类，并提供查询、分类、关键词匹配等
能力。进程内通过 ``instance()`` 单例访问。

设计说明：
    - ``register`` 以类属性 ``name`` 作为唯一键，同名会覆盖旧值。
    - ``get`` 找不到时抛 ``SkillNotFoundError``，由上层路由统一转 404。
    - ``match_trigger`` 采用大小写不敏感的字面子串匹配，用于自然语言
      提示词 → 技能的粗筛（不追求语义匹配）。
"""

from __future__ import annotations

from typing import ClassVar

from skill_engine.base import Skill
from skill_engine.exceptions import SkillNotFoundError


class SkillRegistry:
    """进程内的技能注册表（单例）。"""

    # 单例引用；reset() 后重新构造。
    _instance: ClassVar[SkillRegistry | None] = None
    # 内存存储：skill name -> Skill 子类
    _skills: dict[str, type[Skill]]

    def __init__(self) -> None:
        self._skills = {}

    @classmethod
    def instance(cls) -> SkillRegistry:
        live = cls._instance
        if live:
            return live
        cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """测试/热更新用的重置入口：清空缓存并释放单例。

        若其他代码仍持有旧实例的引用，其 ``_skills`` 也会被清空，
        保证不会读到陈旧的技能列表。
        """
        live = cls._instance
        if live:
            live._purge()
        cls._instance = None

    # ------------------------------------------------------------------
    # 内部存储操作
    # ------------------------------------------------------------------

    def _purge(self) -> None:
        """原地清空存储 —— ``_skills`` 仍是同一个字典对象。

        不能直接 ``self._skills = {}``：外部可能持有旧字典的引用，
        那样清空后它们仍会读到陈旧条目。
        """
        for entry in tuple(self._skills):
            del self._skills[entry]

    def _registered_classes(self) -> list[type[Skill]]:
        """按注册顺序返回已登记的技能类。"""
        return [entry[1] for entry in self._skills.items()]

    # ------------------------------------------------------------------
    # 注册与查询
    # ------------------------------------------------------------------

    def register(self, skill_class: type[Skill]) -> None:
        """把一个技能类加入注册表。

        参数：
            skill_class: 必须定义非空的类属性 ``name``；否则抛 ``ValueError``。
        """
        declared_name = getattr(skill_class, "name", "")
        if declared_name in ("", None):
            raise ValueError(
                f"Skill 类 {skill_class!r} 必须定义非空的 name 属性"
            )
        self._skills[declared_name] = skill_class

    def get(self, name: str) -> type[Skill]:
        """按名称取技能类；未注册抛 ``SkillNotFoundError``。"""
        try:
            return self._skills[name]
        except KeyError:
            raise SkillNotFoundError(f"Skill '{name}' 未注册") from None

    def has(self, name: str) -> bool:
        """是否存在该技能名。"""
        return name in self._skills

    # ------------------------------------------------------------------
    # 列表与匹配
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        """返回所有已注册技能的元数据列表（顺序 = 插入顺序）。"""
        return [_describe_skill(skill_type) for skill_type in self._registered_classes()]

    def list_by_category(self, category: str) -> list[type[Skill]]:
        """返回某个 category 下的技能类列表。"""
        return [
            skill_type
            for skill_type in self._registered_classes()
            if skill_type.category == category
        ]

    def match_trigger(self, text: str) -> list[type[Skill]]:
        """在 ``triggers`` 中做大小写不敏感的子串匹配。

        用于「用户自然语言 → 技能」的粗筛。任一 trigger 命中即入选。
        """
        if text == "":
            return []
        needle = text.casefold()
        return [
            skill_type
            for skill_type in self._registered_classes()
            if _trigger_matches(skill_type, needle)
        ]


# ------------------------------------------------------------------
# 模块级辅助
# ------------------------------------------------------------------


def _trigger_matches(skill_type: type[Skill], needle: str) -> bool:
    """技能的任一触发词是否出现在 ``needle`` 里（大小写不敏感）。"""
    for trigger_word in skill_type.triggers:
        if trigger_word.casefold() in needle:
            return True
    return False


def _describe_skill(skill_type: type[Skill]) -> dict:
    """从 Skill 子类提取公开的元数据字段。"""
    return {
        "name": skill_type.name,
        "description": skill_type.description,
        "category": skill_type.category,
        "version": skill_type.version,
        "triggers": skill_type.triggers,
    }
