"""pricing_check 技能的契约测试。

锁死四件事：
1. 四类检查都在（price_limit / upper_lower_case / arithmetic / safe_range）
2. 每项 status 判定正确（pass / warning / fail）
3. 算术错能报出具体行号与算式
4. 输出 schema 稳定（checks / summary / risk_level）

不依赖网络、不调大模型、不读文件。
"""
from __future__ import annotations

import asyncio

from skill_engine import SkillContext
from skills.pricing_check import PricingCheckSkill


def _run(bid_text: str, max_price: float | None = None):
    """跑一次技能，返回 SkillResult。"""
    params: dict = {"bid_text": bid_text}
    if max_price is not None:
        params["max_price"] = max_price
    ctx = SkillContext(project_id="test", db=None, llm=None, parameters=params)
    return asyncio.run(PricingCheckSkill().execute(ctx))


def _by_type(result) -> dict[str, list[dict]]:
    """按 check_type 归组（同一类型可能有多条）。"""
    out: dict[str, list[dict]] = {}
    for c in result.data["checks"]:
        out.setdefault(c["check_type"], []).append(c)
    return out


# ── 基础契约 ────────────────────────────────────────────────

def test_skill_metadata():
    s = PricingCheckSkill()
    assert s.name == "pricing_check"
    assert s.category == "check"
    assert s.version


def test_output_schema():
    r = _run("投标总价：90万", max_price=100.0)
    assert r.success is True
    assert set(r.data) >= {"checks", "summary", "risk_level"}
    assert set(r.data["summary"]) == {"total", "passed", "failed", "warning"}
    assert r.data["risk_level"] in ("high", "low")
    for c in r.data["checks"]:
        assert set(c) >= {"check_type", "status", "detail"}
        assert c["status"] in ("pass", "warning", "fail")


def test_all_four_checks_present():
    r = _run("投标总价：90万", max_price=100.0)
    types = {c["check_type"] for c in r.data["checks"]}
    assert types == {"price_limit", "upper_lower_case", "arithmetic", "safe_range"}


# ── 1. 最高限价 ────────────────────────────────────────────

def test_price_limit_pass():
    r = _run("投标总价：95万", max_price=100.0)
    assert _by_type(r)["price_limit"][0]["status"] == "pass"


def test_price_limit_fail_when_over():
    r = _run("投标总价：105万", max_price=100.0)
    c = _by_type(r)["price_limit"][0]
    assert c["status"] == "fail"
    assert "超过" in c["detail"]


def test_price_limit_warning_without_cap():
    r = _run("投标总价：95万")
    assert _by_type(r)["price_limit"][0]["status"] == "warning"


# ── 2. 大小写金额 ──────────────────────────────────────────

def test_upper_lower_case_detected():
    """大写金额后紧跟「万元」或「元」时才识别（已知格式局限，见 README 边界）。"""
    r = _run("总金额：壹万元（10000）", max_price=100.0)
    rows = _by_type(r)["upper_lower_case"]
    assert rows[0]["status"] == "pass"
    assert "壹万元" in rows[0]["detail"]


def test_upper_lower_case_long_chinese_amount_detected():
    """多字大写金额也要识别（「玖拾捌万伍仟元」）。

    回归：原正则字符类缺大写数字（壹贰叁…拾仟佰），且「万」写成 `万?`
    只允许末尾一个，导致这类最常见写法匹配不上、静默降级为 warning。
    """
    r = _run("总金额：玖拾捌万伍仟元（985000）", max_price=100.0)
    c = _by_type(r)["upper_lower_case"][0]
    assert c["status"] == "pass"
    assert "玖拾捌万伍仟元" in c["detail"]


def test_upper_lower_case_yi_scale():
    """亿元级金额也要识别。"""
    r = _run("总金额：壹亿元（100000000）", max_price=20000.0)
    c = _by_type(r)["upper_lower_case"][0]
    assert c["status"] == "pass"
    assert "壹亿元" in c["detail"]


def test_upper_lower_case_warning_when_absent():
    r = _run("投标总价：90万", max_price=100.0)
    assert _by_type(r)["upper_lower_case"][0]["status"] == "warning"


# ── 3. 算术核查 ────────────────────────────────────────────

def test_arithmetic_pass():
    r = _run("1. 服务器 12.50 × 8 = 100.00", max_price=200.0)
    assert _by_type(r)["arithmetic"][0]["status"] == "pass"


def test_arithmetic_fail_reports_row_and_math():
    """算错要报出第几项、算式、实际值 —— 用户要能直接定位。"""
    r = _run("2. 交换机 3.20 × 10 = 35.00", max_price=200.0)
    c = _by_type(r)["arithmetic"][0]
    assert c["status"] == "fail"
    assert "第2项" in c["detail"]
    assert "32.00" in c["detail"]   # 期望值
    assert "35.0" in c["detail"]    # 实际值


def test_arithmetic_tolerance_001():
    """误差 ≤ 0.01 视为通过（舍入容差）。"""
    r = _run("1. 项目 1.00 × 3 = 3.00", max_price=200.0)
    assert _by_type(r)["arithmetic"][0]["status"] == "pass"


def test_arithmetic_multiple_rows_only_failures():
    text = """
1. 甲 10.00 × 2 = 20.00
2. 乙 5.00 × 3 = 16.00
3. 丙 8.00 × 4 = 32.00
"""
    r = _run(text, max_price=500.0)
    fails = [c for c in r.data["checks"]
             if c["check_type"] == "arithmetic" and c["status"] == "fail"]
    assert len(fails) == 1
    assert "第2项" in fails[0]["detail"]


# ── 4. 安全区间 ────────────────────────────────────────────

def test_safe_range_pass():
    r = _run("投标总价：96万", max_price=100.0)
    assert _by_type(r)["safe_range"][0]["status"] == "pass"


def test_safe_range_fail_above_band():
    r = _run("投标总价：99万", max_price=100.0)
    c = _by_type(r)["safe_range"][0]
    assert c["status"] == "fail"
    assert "99" in c["detail"]


def test_safe_range_below_band_is_warning_not_fail():
    """低于安全带只提示不判废标 —— 报价低不构成废标风险。

    只有「高于 98%」才是 fail（贴着限价，容易被质疑不合理）。
    """
    r = _run("投标总价：80万", max_price=100.0)
    c = _by_type(r)["safe_range"][0]
    assert c["status"] == "warning"
    assert "低于" in c["detail"]


# ── risk_level 汇总 ────────────────────────────────────────

def test_risk_high_when_any_fail():
    r = _run("投标总价：105万", max_price=100.0)   # 超限价 → fail
    assert r.data["risk_level"] == "high"


def test_risk_low_when_no_fail():
    """只允许 pass / warning，不该被判 high。"""
    r = _run("投标总价：96万", max_price=100.0)
    statuses = {c["status"] for c in r.data["checks"]}
    assert "fail" not in statuses
    assert r.data["risk_level"] == "low"


# ── 容错 ──────────────────────────────────────────────────

def test_empty_text_does_not_crash():
    r = _run("")
    assert r.success is True
    assert r.data["summary"]["total"] == 4


def test_malformed_numbers_skipped():
    """非数字的算术行跳过，不抛异常。"""
    r = _run("1. 项目 abc × def = ghi", max_price=100.0)
    assert r.success is True


def test_summary_counts_consistent():
    r = _run("投标总价：105万", max_price=100.0)
    s = r.data["summary"]
    assert s["total"] == len(r.data["checks"])
    assert s["passed"] + s["failed"] + s["warning"] == s["total"]
