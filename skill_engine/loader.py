"""Skill 引擎 — 技能发现与动态加载。

从磁盘扫描按 ``<root>/<category>/<skill_dir>/{SKILL.md, skill.py}``
布局组织的技能包，把元数据与实现模块缓存到内存，然后按需动态导入
``skill.py`` 并返回其中第一个 ``Skill`` 子类。

典型使用::

    loader = SkillLoader(Path("./skills"))
    loaded = loader.load_all()
    for skill_key in loaded:
        cls = loader.get_skill_class(skill_key)
        SkillRegistry.instance().register(cls)

本模块只负责「从磁盘找到并解析」，不负责把类注册进 registry。
"""

from __future__ import annotations

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from skill_engine.base import Skill
from skill_engine.exceptions import SkillNotFoundError

# SKILL.md front-matter 的分隔符
_FRONTMATTER_MARKER = "---"
# 期望的技能实现文件名
_IMPL_FILENAME = "skill.py"
# 期望的元数据文件名
_METADATA_FILENAME = "SKILL.md"


class SkillLoader:
    """按目录布局加载技能包。"""

    def __init__(self, skills_dir: Path | str) -> None:
        self.skills_dir = Path(skills_dir)
        # 内部索引：skill name -> {"meta": dict, "dir": Path, "module": Path | None}
        self._loaded_skills: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # 目录扫描
    # ------------------------------------------------------------------

    def load_all(self) -> dict[str, dict]:
        """扫描 ``skills_dir`` 下的 ``<category>/<skill_dir>`` 两级目录。

        返回：
            以技能名（来自 SKILL.md 的 ``name`` 字段，缺失时降级为目录名）
            为键的字典，每项包含 ``meta``、``dir``、``module`` 三个字段。
            目录不存在时返回空字典。
        """
        root = self.skills_dir
        if not root.exists():
            return {}

        self._loaded_skills.clear()
        for skill_folder in self._iter_skill_folders(root):
            self._try_index_skill(skill_folder)
        return self._loaded_skills

    def _iter_skill_folders(self, root: Path) -> Iterator[Path]:
        """按「分类目录 → 技能目录」两级顺序产出技能包目录。

        分类目录与技能目录都按名字排序，保证索引顺序稳定可复现。
        """
        for category_folder in sorted(root.iterdir()):
            if not category_folder.is_dir():
                continue
            for skill_folder in sorted(category_folder.iterdir()):
                if skill_folder.is_dir():
                    yield skill_folder

    def _try_index_skill(self, skill_folder: Path) -> None:
        """若 ``skill_folder`` 是合法技能包，则写入内部索引。"""
        metadata_file = skill_folder / _METADATA_FILENAME
        impl_file = skill_folder / _IMPL_FILENAME
        if not (metadata_file.exists() or impl_file.exists()):
            return

        front_matter = (
            self._read_front_matter(metadata_file) if metadata_file.exists() else {}
        )
        skill_name = front_matter.get("name") or skill_folder.name
        self._loaded_skills[skill_name] = {
            "meta": front_matter,
            "dir": skill_folder,
            "module": impl_file if impl_file.exists() else None,
        }

    # ------------------------------------------------------------------
    # 元数据解析
    # ------------------------------------------------------------------

    def _read_front_matter(self, target_path: Path) -> dict:
        """从 ``SKILL.md`` 首段读取 YAML front-matter。

        文件形如::

            ---
            name: xxx
            version: "1.0"
            ---
            正文...

        任何解析失败都优雅降级为 ``{}``，由上层用目录名兜底。
        """
        try:
            text = target_path.read_text(encoding="utf-8")
        except OSError:
            return {}

        if not text.startswith(_FRONTMATTER_MARKER):
            return {}

        chunks = text.split(_FRONTMATTER_MARKER, 2)
        if len(chunks) < 3:
            return {}
        try:
            parsed = yaml.safe_load(chunks[1])
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    # ------------------------------------------------------------------
    # 动态导入
    # ------------------------------------------------------------------

    def get_skill_class(self, name: str) -> type[Skill]:
        """按名称返回该技能实现文件中的第一个 ``Skill`` 子类。

        找不到时抛 ``SkillNotFoundError``，携带定位问题的具体信息。
        """
        record = self._loaded_skills.get(name)
        if record is None:
            raise SkillNotFoundError(f"Skill '{name}' 未找到")

        impl_path = record.get("module")
        if not impl_path or not Path(impl_path).exists():
            raise SkillNotFoundError(f"Skill '{name}' 模块文件不存在")

        loaded_module = self._import_module_from_path(f"skills.{name}", impl_path)
        skill_cls = self._find_skill_subclass(loaded_module)
        if skill_cls is None:
            raise SkillNotFoundError(f"Skill '{name}' 中未找到Skill子类")
        return skill_cls

    @staticmethod
    def _import_module_from_path(qualname: str, target_path: Path):
        """从任意文件路径动态导入模块；失败抛 ``SkillNotFoundError``。"""
        module_spec = importlib.util.spec_from_file_location(qualname, str(target_path))
        if module_spec is None or module_spec.loader is None:
            raise SkillNotFoundError(f"Skill 模块加载失败: {target_path}")
        loaded_module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(loaded_module)  # type: ignore[union-attr]
        return loaded_module

    @staticmethod
    def _find_skill_subclass(loaded_module: Any) -> type[Skill] | None:
        """在模块中搜索第一个非基类本身的 ``Skill`` 子类。"""
        for symbol_name in sorted(dir(loaded_module)):
            candidate = getattr(loaded_module, symbol_name)
            if (
                isinstance(candidate, type)
                and issubclass(candidate, Skill)
                and candidate is not Skill
            ):
                return candidate
        return None
