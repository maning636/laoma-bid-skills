"""规则包读写与合并 —— 纯函数实现。

三函数契约：

- ``load_rules(path) -> list[dict]``
    读取规则包 JSON 文件，返回 ``rules`` 字段。文件缺失或 JSON 解析失败
    抛 ``FileNotFoundError`` / ``ValueError``，不吞异常。

- ``filter_rules(rules, category=None, level=None) -> list[dict]``
    按 category（rules[*].category）与 level（rules[*].legal_level）
    稳定过滤。两者都为 None 时原样返回；任一为 None 表示不过滤该维度。
    入参不被修改。

- ``effective_rules(rules, overrides) -> list[dict]``
    剔除 overrides 中 enabled=False 的 rule_id。overrides 可以是
    dict[str, bool]、ORM 对象列表（含 ``.rule_id`` 与 ``.enabled``）
    或 ``{"rule_id":..., "enabled":...}`` 的 dict 列表。缺 rule_id 的
    项忽略。不修改入参。
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path


def load_rules(path: str | Path) -> list[dict]:
    """读取 rules JSON → 规则列表。

    JSON 顶层期望 ``{"version": ..., "generated_at": ..., "rules": [...]}``；
    仅返回 ``rules`` 字段。文件缺失、非 JSON、缺 ``rules`` 键或 ``rules``
    非 list 都抛异常，让上游决定 4xx/5xx。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"规则包文件不存在: {p}")
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"规则包 JSON 解析失败: {e}") from e
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise ValueError("规则包 JSON 结构不合法：缺 rules[]")  # noqa: TRY004
    return payload["rules"]


def filter_rules(
    rules: Iterable[dict],
    category: str | None = None,
    level: str | None = None,
) -> list[dict]:
    """按 category / legal_level 过滤规则。

    字段名对齐规则包 JSON：
        - ``category``  → ``rules[*].category``
        - ``level``     → ``rules[*].legal_level``
    """
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if category is not None and r.get("category") != category:
            continue
        if level is not None and r.get("legal_level") != level:
            continue
        out.append(r)
    return out


def _override_is_disabled(item: object) -> bool:
    """从 ORM 行 / dict / 属性对象上安全读 (rule_id, enabled)。"""
    enabled: object
    rule_id: object
    if isinstance(item, dict):
        rule_id = item.get("rule_id")
        enabled = item.get("enabled")
    else:
        rule_id = getattr(item, "rule_id", None)
        enabled = getattr(item, "enabled", None)
    if rule_id is None:
        return False
    return enabled is False


def effective_rules(
    rules: Iterable[dict],
    overrides: Iterable[object],
) -> list[dict]:
    """剔除 overrides 中 enabled=False 的 rule_id。"""
    disabled: set[str] = set()
    for item in overrides:
        # 快速路径：先用 dict / 属性探测拿 rule_id
        if isinstance(item, dict):
            rule_id = item.get("rule_id")
            enabled = item.get("enabled")
        else:
            rule_id = getattr(item, "rule_id", None)
            enabled = getattr(item, "enabled", None)
        if rule_id is None:
            continue
        if enabled is False:
            disabled.add(str(rule_id))
    if not disabled:
        return [r for r in rules if isinstance(r, dict)]
    out: list[dict] = []
    for r in rules:
        if not isinstance(r, dict):
            continue
        if str(r.get("rule_id")) in disabled:
            continue
        out.append(r)
    return out
