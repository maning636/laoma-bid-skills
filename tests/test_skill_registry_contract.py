"""Skill 注册表级契约测试（迁移到开源仓版本）。

来源：主仓 `tests/test_skill_registry_contract.py`，只迁移了**第 1 节
「SkillRegistry 本体契约」**（8 个用例），因为它只依赖 `skill_engine`
的 base / registry / exceptions，不涉及主仓的 `services.skill_bootstrap`。

未迁移部分（原文件第 2~5 节，共 10+ 个用例）：
- 依赖 ``services.skill_bootstrap.register_builtin_skills()`` 注册的 46 个
  业务 skill 的元数据契约（name/category/triggers 等）与 18 个未覆盖
  skill 文件的显式参数化 —— 这些 skill 属于主仓 `services/`，开源仓无
  等价物，**本用例依赖主仓 services.skill_bootstrap，未迁**。
"""

from __future__ import annotations

from typing import ClassVar

import pytest

from skill_engine.base import Skill, SkillResult
from skill_engine.exceptions import SkillNotFoundError
from skill_engine.registry import SkillRegistry


@pytest.fixture(autouse=True)
def _fresh_registry():
    """每个用例开始前都 reset 单例，避免用例间残留污染。"""
    SkillRegistry.reset()
    yield
    SkillRegistry.reset()


class _DummySkill(Skill):
    name = "dummy"
    description = "占位 skill"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["dummy"]

    async def execute(self, ctx):  # pragma: no cover - 契约测试不执行
        return SkillResult(success=True)


class _DummySkillOther(_DummySkill):
    name = "dummy"
    description = "占位 skill —— 同名不同类"


class _NoNameSkill(Skill):
    name = ""
    category = "check"
    version = "1.0.0"

    async def execute(self, ctx):  # pragma: no cover
        return SkillResult(success=True)


def test_registry_is_singleton():
    """同一个进程内两次 instance() 必须返回同一对象。"""
    a = SkillRegistry.instance()
    b = SkillRegistry.instance()
    assert a is b


def test_reset_clears_singleton_and_skills():
    """reset() 后是全新实例，且旧实例的 _skills 被清空。"""
    old = SkillRegistry.instance()
    old.register(_DummySkill)
    assert old.has("dummy")
    SkillRegistry.reset()
    new = SkillRegistry.instance()
    assert new is not old
    assert not new.has("dummy")
    # 旧实例的存储也应当被清空（源码显式行为）
    assert not old.has("dummy")


def test_register_requires_non_empty_name():
    with pytest.raises(ValueError):
        SkillRegistry.instance().register(_NoNameSkill)


def test_register_overwrites_same_name():
    """同名重复注册：后注册者覆盖前者（源码语义），不报错。"""
    reg = SkillRegistry.instance()
    reg.register(_DummySkill)
    assert reg.get("dummy") is _DummySkill
    reg.register(_DummySkillOther)
    assert reg.get("dummy") is _DummySkillOther
    # 覆盖后 list_all() 仍只有 1 条
    assert len(reg.list_all()) == 1


def test_get_hit_and_miss():
    reg = SkillRegistry.instance()
    reg.register(_DummySkill)
    assert reg.get("dummy") is _DummySkill
    with pytest.raises(SkillNotFoundError):
        reg.get("not-registered")


def test_has_returns_boolean_membership():
    reg = SkillRegistry.instance()
    reg.register(_DummySkill)
    assert reg.has("dummy") is True
    assert reg.has("nope") is False


def test_list_by_category_filters_exactly():
    """category 精确匹配；不区分前后缀。"""
    reg = SkillRegistry.instance()
    reg.register(_DummySkill)  # category='check'
    assert reg.list_by_category("generate") == []
    assert reg.list_by_category("check") == [_DummySkill]


def test_match_trigger_case_insensitive_substring():
    """真实触发词 'dummy' 命中，大小写不敏感。"""
    reg = SkillRegistry.instance()
    reg.register(_DummySkill)
    assert reg.match_trigger("我需要一个 Dummy 检查工具") == [_DummySkill]
    assert reg.match_trigger("DUMMY 大写也一样") == [_DummySkill]
    assert reg.match_trigger("无关文案") == []
    assert reg.match_trigger("") == []
