"""报价算术核查 Skill。

覆盖四类常见废标风险：
1. 最高限价：投标总价是否 ≤ 最高限价。
2. 大小写金额对照：`（一万元）` 一类的对照是否存在。
3. 单位×数量=合价：单行算术是否自洽。
4. 安全区间：报价/限价比例是否落在 95%~98% 的安全带。

`execute` 保持原有 data schema：`{checks, summary, risk_level}`；
每项检查项以 `{"check_type", "status", "detail"}` 结构返回，
`status ∈ {"pass", "warning", "fail"}`。
"""

from __future__ import annotations

import re
from typing import ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

from .evidence import EVIDENCE_VERDICT, make_evidence

_PRICING_BASIS = "最高限价 / 大小写金额对照 / 单位×数量=合价 / 安全区间四类算术规则"

# 匹配 "投标总价: 123.45万" 一类行；`万?元?` 允许省略单位。
_TOTAL_PATTERN = re.compile(r"投标总价[：:]\s*([\d,，.]+)\s*万?元?")

# 匹配 "总金额：壹万元（10000）" 的大写-小写对照。
_CASE_PATTERN = re.compile(
    # 大写金额 = 大写数字/小写数字/万/亿 的连续串 + 结尾「元」，后跟（小写数字）
    #
    # 三个曾踩的坑（都导致「壹万元」这类最常见写法匹配不上）：
    # 1. 字符类里原本**没有大写数字**（壹贰叁…拾仟佰），只有小写数字；
    # 2. 把「万」写成 `万?`（只允许一个且在末尾）→「玖拾捌万伍仟元」断在「伍仟元」；
    # 3. 把「万」放进字符类但漏了「亿」→ 亿元级金额匹配不上。
    #
    # 正确做法：万/亿 与其他数字字一起放进字符类（允许出现在中间），
    # 末尾单独要求「元」。
    r"([零壹贰叁肆伍陆柒捌玖一二三四五六七八九十拾百佰千仟万亿]+元)"
    r"\s*[（(]\s*([\d,，.]+)\s*[）)]"
)

# 匹配 "1. 单价 12.34 × 数量 5 = 61.70" 的算术行。
_ARITH_ROW_PATTERN = re.compile(
    r"(\d+)\s*[、．.]\s*(.+?)\s+([\d,，.]+)\s*[×xX]\s*([\d,，.]+)\s*=\s*([\d,，.]+)"
)

_SAFE_LOW = 0.95
_SAFE_HIGH = 0.98


def _to_float(raw: str) -> float:
    """把带千分位/中文逗号的数字文本转成 float。"""
    return float(raw.replace(",", "").replace("，", ""))


def _read_total(bid_text: str) -> float | None:
    """从正文中提取投标总价；未命中返回 None。"""
    matched = _TOTAL_PATTERN.search(bid_text)
    if not matched:
        return None
    return _to_float(matched.group(1))


def _limit_check(bid_total: float | None, max_price: float | None) -> dict:
    if max_price is None:
        return {"check_type": "price_limit", "status": "warning", "detail": "未提供最高限价"}
    if bid_total is None:
        return {"check_type": "price_limit", "status": "warning", "detail": "未找到投标总价"}
    if bid_total > max_price:
        return {
            "check_type": "price_limit",
            "status": "fail",
            "detail": f"投标总价{bid_total}万超过最高限价{max_price}万",
        }
    return {
        "check_type": "price_limit",
        "status": "pass",
        "detail": f"投标总价{bid_total}万未超过限价{max_price}万",
    }


def _case_checks(bid_text: str) -> list[dict]:
    """大小写金额对照：仅检测是否存在，一致性仍需人工确认。"""
    rows = [
        {
            "check_type": "upper_lower_case",
            "status": "pass",
            "detail": f"大写:{m.group(1)} 小写:{m.group(2)}元 (需人工确认一致)",
        }
        for m in _CASE_PATTERN.finditer(bid_text)
    ]
    if not rows:
        rows.append(
            {"check_type": "upper_lower_case", "status": "warning", "detail": "未检测到大小写金额对照"}
        )
    return rows


def _arithmetic_checks(bid_text: str) -> list[dict]:
    """逐行验证 `单价 × 数量 = 合价`，仅报告算术不符的行。"""
    failures: list[dict] = []
    for match in _ARITH_ROW_PATTERN.finditer(bid_text):
        try:
            unit_price = _to_float(match.group(3))
            quantity = _to_float(match.group(4))
            actual = _to_float(match.group(5))
        except ValueError:
            continue
        expected = unit_price * quantity
        if abs(expected - actual) > 0.01:
            failures.append(
                {
                    "check_type": "arithmetic",
                    "status": "fail",
                    "detail": f"第{match.group(1)}项: {unit_price}×{quantity}={expected:.2f}≠{actual}",
                }
            )
    if not failures:
        failures.append(
            {"check_type": "arithmetic", "status": "pass", "detail": "算术验算通过(或未检测到算术行)"}
        )
    return failures


def _safe_range_check(bid_total: float | None, max_price: float | None) -> dict:
    if max_price is None:
        return {"check_type": "safe_range", "status": "warning", "detail": "未提供限价，无法判断安全区间"}
    if max_price == 0:
        return {"check_type": "safe_range", "status": "warning", "detail": "限价为0，无法计算安全区间"}
    if bid_total is None:
        return {"check_type": "safe_range", "status": "warning", "detail": "未找到投标总价"}
    ratio = bid_total / max_price
    if _SAFE_LOW <= ratio <= _SAFE_HIGH:
        return {
            "check_type": "safe_range",
            "status": "pass",
            "detail": f"报价在限价{ratio:.1%}，处于安全区间(95%-98%)",
        }
    if ratio < _SAFE_LOW:
        return {
            "check_type": "safe_range",
            "status": "warning",
            "detail": f"报价仅为限价{ratio:.1%}，低于95%安全线",
        }
    return {
        "check_type": "safe_range",
        "status": "fail",
        "detail": f"报价为限价{ratio:.1%}，超过98%",
    }


def _summarize(checks: list[dict]) -> dict:
    """聚合 pass/warning/fail 计数与整体 risk_level。"""
    passed = sum(1 for c in checks if c["status"] == "pass")
    failed = sum(1 for c in checks if c["status"] == "fail")
    warning = sum(1 for c in checks if c["status"] == "warning")
    return {
        "total": len(checks),
        "passed": passed,
        "failed": failed,
        "warning": warning,
    }


class PricingCheckSkill(Skill):
    name = "pricing_check"
    description = "报价算术核查(废标预防4)"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["报价", "算术", "价格核查"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        tender_text = ctx.parameters.get("tender_text", "")  # noqa: F841 —— 保留以兼容契约
        bid_text = ctx.parameters.get("bid_text", "")
        max_price = ctx.parameters.get("max_price")

        bid_total = _read_total(bid_text)

        checks: list[dict] = [_limit_check(bid_total, max_price)]
        checks.extend(_case_checks(bid_text))
        checks.extend(_arithmetic_checks(bid_text))
        checks.append(_safe_range_check(bid_total, max_price))

        # 范式：pricing_check 是纯规则可判定（确定性算术），显式传 evidence_level=verdict
        for c in checks:
            if isinstance(c, dict):
                c.update(make_evidence(EVIDENCE_VERDICT, _PRICING_BASIS))

        return SkillResult(
            success=True,
            data={
                "checks": checks,
                "summary": _summarize(checks),
                "risk_level": "high" if any(c["status"] == "fail" for c in checks) else "low",
            },
        )
