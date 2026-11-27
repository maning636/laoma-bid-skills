"""废标自查清单 Skill。

读取一批下游 check skill 的原始输出，按预置的 SC 编号检查项逐条折算成
`pass / fail / pending` 三态，最后聚合出投标文件能不能提交的结论。

检查项来自 `SELFCHECK_ITEMS`：资质、保证金、文件与签章、技术与报价、
有效期与合规五个大类，一共 20 条（SC-01 ~ SC-20）。

⚠️ 业务数据不可删减：SC 编号、归属的 check_skill 以及每条的判定含义
必须保持稳定；要扩充时只允许追加，不得重排或删除既有条目。
"""

from __future__ import annotations

from typing import ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

# 每个分类下面挂一组 SC 编号项；`check_skill` 与对应下游 skill 的 name 对齐。
SELFCHECK_ITEMS = [
    {
        "category": "资质文件核查",
        "items": [
            {"id": "SC-01", "content": "所有资质证书有效期覆盖投标截止日", "check_skill": "qualification_check"},
            {"id": "SC-02", "content": "证书名称与招标要求完全一致", "check_skill": "qualification_check"},
            {"id": "SC-03", "content": "三证合一/旧版分离证书匹配", "check_skill": "qualification_check"},
            {"id": "SC-04", "content": "证书扫描件清晰可辨", "check_skill": "qualification_check"},
        ],
    },
    {
        "category": "投标保证金核查",
        "items": [
            {"id": "SC-05", "content": "保证金金额与招标要求一致(精确到分)", "check_skill": "deposit_check"},
            {"id": "SC-06", "content": "缴纳形式符合要求", "check_skill": "deposit_check"},
            {"id": "SC-07", "content": "到账时间早于截止时间", "check_skill": "deposit_check"},
            {"id": "SC-08", "content": "保函有效期覆盖投标有效期+30天", "check_skill": "deposit_check"},
        ],
    },
    {
        "category": "文件完整性与签章核查",
        "items": [
            {"id": "SC-09", "content": "正本副本份数与要求一致", "check_skill": "doc_integrity"},
            {"id": "SC-10", "content": "所有要求签章页面已签章", "check_skill": "signature_check"},
            {"id": "SC-11", "content": "密封袋标识规范", "check_skill": "doc_integrity"},
            {"id": "SC-12", "content": "骑缝章完整", "check_skill": "signature_check"},
        ],
    },
    {
        "category": "技术与报价核查",
        "items": [
            {"id": "SC-13", "content": "所有★▲强制性参数已响应且无负偏离", "check_skill": "mandatory_req_check"},
            {"id": "SC-14", "content": "报价算术无误(单价×数量=合价)", "check_skill": "pricing_check"},
            {"id": "SC-15", "content": "投标总价未超过最高限价", "check_skill": "pricing_check"},
            {"id": "SC-16", "content": "大写金额与小写金额一致", "check_skill": "pricing_check"},
        ],
    },
    {
        "category": "有效期与合规核查",
        "items": [
            {"id": "SC-17", "content": "投标有效期≥招标要求天数", "check_skill": "validity_check"},
            {"id": "SC-18", "content": "保函有效期覆盖投标有效期", "check_skill": "validity_check"},
            {"id": "SC-19", "content": "CA证书在有效期内", "check_skill": "validity_check"},
            {"id": "SC-20", "content": "跨章节数据一致性校验通过", "check_skill": "consistency_check"},
        ],
    },
]

_FAIL_RISK_LEVELS = {"high", "critical"}


def _downstream_passed(skill_resp: dict) -> bool:
    """判断下游 check skill 的输出是否代表"通过"。

    判定顺序与历史实现保持一致：
      - 输出是空对象 —— 不通过；
      - `has_critical_issues` 为真 —— 不通过；
      - `risk_level` 落在 high / critical —— 不通过；
      - `summary.failed` 大于 0 —— 不通过；
      - 以上都不命中 —— 通过。
    """
    if not skill_resp:
        return False
    if skill_resp.get("has_critical_issues"):
        return False
    if skill_resp.get("risk_level", "") in _FAIL_RISK_LEVELS:
        return False
    summary_block = skill_resp.get("summary")
    return not (isinstance(summary_block, dict) and summary_block.get("failed", 0) > 0)


def _resolve_status(check_outputs: dict, skill_key: str) -> str:
    """把下游 skill 的输出折算成 pass / fail / pending 之一。"""
    downstream_data = check_outputs.get(skill_key)
    if downstream_data is None:
        return "pending"
    return "pass" if _downstream_passed(downstream_data) else "fail"


def _selfcheck_item_row(check_outputs: dict, item_def: dict) -> tuple[dict, str]:
    """把一条检查项渲染成结果行，并一并返回它的状态。"""
    row_status = _resolve_status(check_outputs, item_def["check_skill"])
    rendered = {
        "id": item_def["id"],
        "content": item_def["content"],
        "status": row_status,
        "check_skill": item_def["check_skill"],
    }
    return rendered, row_status


def _selfcheck_category_rows(check_outputs: dict, cat_group: dict) -> dict:
    """汇总一个分类下的全部检查项。"""
    item_list: list[dict] = []
    group_total = 0
    group_passed = 0
    group_clear = True

    for item_def in cat_group["items"]:
        rendered, row_status = _selfcheck_item_row(check_outputs, item_def)
        item_list.append(rendered)
        group_total += 1
        if row_status == "pass":
            group_passed += 1
        else:
            group_clear = False

    return {
        "category": cat_group["category"],
        "items": item_list,
        "total": group_total,
        "passed": group_passed,
        "clear": group_clear,
    }


class SelfcheckListSkill(Skill):
    name = "selfcheck_list"
    description = "废标自查清单(综合预防)"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["自查", "废标自查", "提交前检查"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        check_outputs = ctx.parameters.get("check_results", {})

        cat_list: list[dict] = []
        all_pass = True
        total_count = 0
        pass_count = 0

        for cat_group in SELFCHECK_ITEMS:
            group = _selfcheck_category_rows(check_outputs, cat_group)
            cat_list.append({"category": group["category"], "items": group["items"]})
            total_count += group["total"]
            pass_count += group["passed"]
            if not group["clear"]:
                all_pass = False

        return SkillResult(
            success=True,
            data={
                "categories": cat_list,
                "all_passed": all_pass,
                "can_submit": all_pass,
                "total_items": total_count,
                "passed_items": pass_count,
            },
        )
