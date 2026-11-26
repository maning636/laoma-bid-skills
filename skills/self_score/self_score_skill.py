"""自评分估算 — F9 提交前预估分数+薄弱项标注"""
from __future__ import annotations

import logging
from typing import ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)


class SelfScoreSkill(Skill):
    name = "self_score"
    description = "自评分估算(F9): LLM逐项估分+薄弱项标注"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["自评分", "预估分数", "评分估算"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        scoring_rows = ctx.parameters.get("scoring_rows", [])
        blocks = ctx.parameters.get("blocks", [])
        if not scoring_rows:
            return SkillResult(success=False, error="评分矩阵为空")

        # 为每个评分项估算得分
        items = []
        total_estimated = 0
        total_max = 0

        for sr in scoring_rows:
            item_name = sr.get("item", "")
            max_score = float(sr.get("score", 0))
            total_max += max_score

            # 找到关联的块
            related = [b for b in blocks if item_name and (
                item_name in str(b.get("score_items", [])) or
                any(kw in b.get("title", "") for kw in item_name.split()[:2] if len(kw) > 2)
            )]

            if not related:
                estimated = 0
                reason = "未找到对应章节"
            else:
                best = max(related, key=lambda b: b.get("content_length", 0))
                length = best.get("content_length", 0)
                if length >= 500:
                    estimated = round(max_score * 0.9, 1)
                    reason = f"内容充实（{length}字），预估得{estimated}/{max_score}"
                elif length >= 200:
                    estimated = round(max_score * 0.7, 1)
                    reason = f"内容中等（{length}字），预估得{estimated}/{max_score}"
                elif length > 0:
                    estimated = round(max_score * 0.4, 1)
                    reason = f"内容不足（{length}字），预估得{estimated}/{max_score}"
                else:
                    estimated = 0
                    reason = "对应章节为空，预估得0分"

            total_estimated += estimated
            items.append({
                "item": item_name,
                "category": sr.get("category", ""),
                "max_score": max_score,
                "estimated_score": estimated,
                "loss": round(max_score - estimated, 1),
                "reason": reason,
                "is_weak": estimated < max_score * 0.6,
            })

        # 薄弱项（预计丢分较多的前5项）
        weak_items = sorted([i for i in items if i["is_weak"]], key=lambda x: x["loss"], reverse=True)[:5]

        return SkillResult(
            success=True,
            data={
                "items": items,
                "total_max": total_max,
                "total_estimated": round(total_estimated, 1),
                "estimated_percentage": round(total_estimated / total_max * 100, 1) if total_max > 0 else 0,
                "weak_items": weak_items,
                "weak_count": len(weak_items),
                "recommendation": f"预估总分{round(total_estimated,1)}/{total_max}（{round(total_estimated/total_max*100,1) if total_max>0 else 0}%），"
                                  + (f"薄弱项{len(weak_items)}个，建议加强：{', '.join(i['item'] for i in weak_items[:3])}" if weak_items else "各评分项覆盖均衡"),
            },
        )
