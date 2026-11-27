"""风险评分 Skill（C-08）。

对上游六类检查结果做加权综合：

    field_overlap     0.20  字段重叠度（关键信息一致性）
    price             0.20  价格风险（超限价、异常低价）
    metadata          0.15  元数据风险（作者、修改时间等）
    structure         0.15  结构完整性（章节缺失）
    text              0.20  文本质量（错别字、表述一致性）
    rare_sequences    0.10  异常序列（隐藏字符、可疑编码）

综合得分 ≥70 → high；≥40 → medium；否则 low。任一维度 ≥70 都进入
`summary.critical_dimensions`，并在 recommendations 中列出整改提示。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ClassVar

from skill_engine.base import Skill, SkillContext, SkillResult

_DIMENSION_WEIGHTS: dict[str, float] = {
    "field_overlap": 0.20,
    "price": 0.20,
    "metadata": 0.15,
    "structure": 0.15,
    "text": 0.20,
    "rare_sequences": 0.10,
}

_DIMENSION_DESCRIPTIONS: dict[str, str] = {
    "field_overlap": "字段重叠度: 投标文件与招标文件关键字段(项目名称、编号、金额等)的一致性",
    "price": "价格风险: 报价合理性、是否超限价、是否异常低价",
    "metadata": "元数据风险: 文档属性、作者信息、修改时间等元数据异常",
    "structure": "结构完整性: 文档章节结构是否完整、是否缺失必要章节",
    "text": "文本质量: 错别字、标点、表述一致性等文本问题",
    "rare_sequences": "异常序列: 文档中是否存在异常字符序列、隐藏内容、可疑编码",
}

_CRITICAL_THRESHOLD = 70.0
_HIGH_BAND = 70.0
_MEDIUM_BAND = 40.0
_DEFAULT_SCORE = 50.0


def _clamp_100(value: float) -> float:
    """把风险分值收敛到 [0, 100]。"""
    return max(0.0, min(100.0, value))


def _field_overlap_score(data: dict) -> float:
    """以失败项占比换算。"""
    checks = data.get("checks", [])
    if not checks:
        return float(data.get("score", _DEFAULT_SCORE))
    failed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "fail")
    return _clamp_100(round(failed / max(len(checks), 1) * 100, 2))


def _price_score(data: dict) -> float:
    """失败项按两倍权重，warning 按一倍。"""
    checks = data.get("checks", [])
    if not checks:
        return float(data.get("score", _DEFAULT_SCORE))
    failed = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "fail")
    warning = sum(1 for c in checks if isinstance(c, dict) and c.get("status") == "warning")
    return _clamp_100(round((failed * 2 + warning) / max(len(checks), 1) * 50, 2))


def _metadata_score(data: dict) -> float:
    anomalies = data.get("anomalies", [])
    if not anomalies:
        return float(data.get("score", 20.0))
    return _clamp_100(len(anomalies) * 25)


def _structure_score(data: dict) -> float:
    missing = data.get("missing_sections", [])
    if not missing:
        return float(data.get("score", 10.0))
    return _clamp_100(len(missing) * 15)


def _text_score(data: dict) -> float:
    issues = data.get("issues", [])
    if not issues:
        return float(data.get("score", 10.0))
    high = sum(1 for i in issues if isinstance(i, dict) and i.get("severity") == "high")
    medium = sum(1 for i in issues if isinstance(i, dict) and i.get("severity") == "medium")
    low_tail = len(issues) - high - medium
    return _clamp_100(high * 20 + medium * 8 + low_tail * 3)


def _rare_sequences_score(data: dict) -> float:
    sequences = data.get("rare_sequences", [])
    if not sequences:
        return float(data.get("score", 5.0))
    return _clamp_100(len(sequences) * 30)


_SCORERS: dict[str, Callable[..., float]] = {
    "field_overlap": _field_overlap_score,
    "price": _price_score,
    "metadata": _metadata_score,
    "structure": _structure_score,
    "text": _text_score,
    "rare_sequences": _rare_sequences_score,
}


def _band_label(score: float) -> str:
    if score >= _HIGH_BAND:
        return "高风险"
    if score >= _MEDIUM_BAND:
        return "中风险"
    return "低风险"


def _band_tail(label: str) -> str:
    return {
        "高风险": "存在严重问题",
        "中风险": "存在部分问题",
        "低风险": "基本正常",
    }[label]


def _overall_level(composite_score: float) -> str:
    if composite_score >= _HIGH_BAND:
        return "high"
    if composite_score >= _MEDIUM_BAND:
        return "medium"
    return "low"


def _recommendations(
    dimension_scores: dict[str, dict],
    risk_level: str,
) -> list[str]:
    """按整体等级 + 高严重维度，拼一份整改建议列表。"""
    tips: list[str] = []
    if risk_level == "high":
        tips.append("综合风险较高，建议全面复核投标文件后再行提交")
    elif risk_level == "medium":
        tips.append("存在中等风险，建议针对高风险维度进行重点修改")

    for dim_id, payload in dimension_scores.items():
        if payload["score"] >= _CRITICAL_THRESHOLD:
            tips.append(
                f"【{_DIMENSION_DESCRIPTIONS[dim_id]}】"
                f"得分{payload['score']}，需重点整改"
            )

    if not tips:
        tips.append("整体风险较低，建议做常规复核后提交")
    return tips


class RiskScoreSkill(Skill):
    name = "risk_score"
    description = "风险评分(C-08): 6维度加权风险评分"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["风险评分", "风险打分", "综合风险"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        check_results = ctx.parameters.get("check_results", {})
        if not check_results:
            return SkillResult(success=False, error="检查结果数据为空，无法进行风险评分")

        dimension_scores: dict[str, dict] = {}
        for dim_id, weight in _DIMENSION_WEIGHTS.items():
            dim_data = check_results.get(dim_id, {})
            scorer = _SCORERS[dim_id]
            raw_score = scorer(dim_data) if dim_data else _DEFAULT_SCORE

            band = _band_label(raw_score)
            dim_description = _DIMENSION_DESCRIPTIONS[dim_id]
            dimension_scores[dim_id] = {
                "score": raw_score,
                "weight": weight,
                "weighted_score": round(raw_score * weight, 2),
                "description": dim_description,
                "detail": f"{band}: {dim_description}{_band_tail(band)}",
            }

        composite_score = round(
            sum(payload["weighted_score"] for payload in dimension_scores.values()), 2
        )
        risk_level = _overall_level(composite_score)

        return SkillResult(
            success=True,
            data={
                "composite_score": composite_score,
                "risk_level": risk_level,
                "dimensions": dimension_scores,
                "summary": {
                    "highest_risk": max(
                        dimension_scores, key=lambda k: dimension_scores[k]["score"]
                    ),
                    "lowest_risk": min(
                        dimension_scores, key=lambda k: dimension_scores[k]["score"]
                    ),
                    "critical_dimensions": [
                        key
                        for key, payload in dimension_scores.items()
                        if payload["score"] >= _CRITICAL_THRESHOLD
                    ],
                },
                "recommendations": _recommendations(dimension_scores, risk_level),
            },
        )
