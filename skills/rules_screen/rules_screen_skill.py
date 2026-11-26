"""法规规则筛查 Skill。

按 `data/rules/rules_v1.json` 的规则库对投标/招标文本做关键词筛查：

- ``legal_level == "verdict"`` 的规则：命中 → 报「命中法规条款」(``status="hit"``)，
  未命中 → ``status="clear"``（依据分级 ``checked=True``）。
- ``legal_level == "hint"`` 的规则：命中 → ``status="note"``（提示性，非硬性违规）。

关键词来源（分两路）：

1. ``verdict`` 规则：使用模块常量 ``RULE_KEYWORDS`` 手工配置的关键中文短语，
   避免对自动切词的依赖，保证核心硬性条款的可控命中。
2. 其余 ``hint`` 规则：从 ``title`` + ``check_hint`` 切出 ≥2 字的中文词组，
   去重后作为关键词集合。

规则包路径解析顺序（与 ``services/routers/rules.py`` 保持一致）：

1. 环境变量 ``BMP_RULES_JSON_PATH``；
2. ``data_root()/data/rules/rules_v1.json``；
3. ``app_root()/data/rules/rules_v1.json``。

响应载荷：

- ``items``：全部规则条目（每条含 rule_id/title/status/evidence_level/basis/source_url）
- ``summary``：``{hit, note, clear, total}``
- ``rules_path``：本次使用的规则包路径
- 顶层 ``enrich_evidence`` 会补齐 ``evidence_summary``，本 skill 内已显式声明
  ``evidence_level`` 与 ``basis``，汇聚层尊重原值（见 ``_evidence._enrich_item``）。
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import ClassVar

from pathkit import app_root, data_root
from rules_engine import load_rules
from skill_engine.base import Skill, SkillContext, SkillResult

from ..pricing_check.evidence import enrich_evidence

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────
_RULES_VERSION = "v1"
_RULES_FILENAME = f"rules_{_RULES_VERSION}.json"
_ENV_RULES_PATH = "BMP_RULES_JSON_PATH"

_STATUS_HIT = "hit"
_STATUS_NOTE = "note"
_STATUS_CLEAR = "clear"

_BASIS_HIT = "命中规则库关键词 → 报告命中法规条款"
_BASIS_NOTE = "命中规则库关键词 → 提示性匹配，需人工判断"
_BASIS_CLEAR = "规则库硬性条款未在文本中匹配到关键词 → 已核验通过"
_BASIS_NO_MATCH = "规则库提示性条款未在文本中匹配到关键词 → 已核验通过"

_DEFAULT_BASIS = "法规规则筛查（依据 rules_v1 规则库关键词匹配）"

#: 每个 verdict 规则手工配置的关键词。至少命中 1 个才算命中。
#: 关键词都是中文名词/名词性短语，避免与通用词重合。
RULE_KEYWORDS: dict[str, list[str]] = {
    "1": ["预算资金", "国有企业事业单位", "国有资金", "进入 16 号令"],
    "2": ["世界银行", "亚洲开发银行", "外国政府", "国际组织贷款", "援助资金"],
    "3": ["公用事业", "基础设施", "843 号文", "范围授权"],
    "4": ["施工单项合同估算价", "400 万元", "施工类合并"],
    "5": ["重要设备", "材料等货物", "200 万元"],
    "6": ["勘察", "设计", "监理", "100 万元"],
    "7": ["门槛汇总", "三档", "规模标准"],
    "8": ["合并采购", "合并进行", "规避"],
    "9": ["2018-06-01", "施行日期", "生效日"],
    "18": ["400 万元以下", "843 号文", "未达到 16 号令"],
    "R-1001": ["投标函", "投标报价", "施工组织设计", "商务和技术偏差表"],
    "R-1002": ["实质性要求", "偏差表", "偏差说明"],
    "R-1020": ["法定代表人", "公章", "电子签章", "授权代理人"],
    "R-1021": ["共同投标协议", "联合体", "授权书"],
    "R-1030": ["投标保证金", "2%", "基本账户", "投标有效期"],
    "R-1031": ["80 万元", "613 号条例", "30 号令"],
    "R-1032": ["银行保函", "银行汇票", "现金支票", "保证金凭证"],
    "R-1041": ["单价", "工程量", "总价", "大小写"],
    "R-1050": ["营业执照", "资质证书", "安全生产许可证", "财务审计报告", "业绩证明"],
    "R-1051": ["资格预审", "组织机构", "联合体成员名称"],
    "R-1061": ["施工组织设计", "施工方案", "进度计划", "分包计划"],
    "R-1072": ["CA 数字证书", "电子签名", "电子印章", "电子交易平台"],
    "R-1073": ["CA 证书加密", "视为撤销", "视为撤回"],
    "R-1080": ["密封", "签收凭证", "骑缝章"],
    "R-1081": ["交易平台", "补充/修改/撤回"],
    "R-1090": ["投标有效期"],
    "R-1100": ["最短时限", "编制期合规"],
    "R-1101": ["逾期送达", "补充递交"],
    "R-1110": ["废标", "否决投标"],
    "R-1111": ["同一招标项目报两个或多个", "字迹清晰"],
    "R-1120": ["采购预算", "政府采购"],
    "R-1121": ["采购文件", "15 年"],
}


# ── 路径与切词 ─────────────────────────────────────────────────────────


def _candidate_rules_paths() -> list[Path]:
    """与 ``services/routers/rules.py::_candidate_rules_paths`` 语义一致。"""
    env_override = os.environ.get(_ENV_RULES_PATH)
    cands: list[Path] = []
    if env_override:
        cands.append(Path(env_override))
    for root in (data_root(), app_root()):
        cands.append(root / "data" / "rules" / _RULES_FILENAME)
    return cands


def _resolve_rules_path() -> Path:
    for p in _candidate_rules_paths():
        if p.is_file():
            return p
    return _candidate_rules_paths()[0]


# 简单切词：连续中文 ≥2 字视为一个词组；中英文/数字/标点作分隔符。
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

# 常见通用词（避免噪声命中）
_STOPWORDS = frozenset({
    "投标", "投标书", "招标", "招标文件", "投标文件", "应当", "可以", "必须",
    "不得", "符合", "规定", "要求", "根据", "按照", "参照", "本项目", "相关", "有关", "以上", "以下", "等类", "类项", "情形", "条件", "项目",
    "上述", "本条", "本规定", "中标", "采购人", "招标方",
})


def _split_chinese_phrases(text: str) -> list[str]:
    """从文本切出所有 ≥2 字的中文词组（去重、去停用词）。"""
    if not text:
        return []
    tokens = _TOKEN_RE.findall(text)
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        # 过滤：过短 / 停用词 / 全部落在通用白名单
        if len(t) < 2:
            continue
        if t in _STOPWORDS:
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _keywords_for_rule(rule: dict) -> list[str]:
    """按 legal_level 决定关键词来源：verdict 用 RULE_KEYWORDS，其余用切词。"""
    rule_id = str(rule.get("rule_id") or "")
    level = rule.get("legal_level")
    if level == "verdict" and rule_id in RULE_KEYWORDS:
        return list(RULE_KEYWORDS[rule_id])
    # hint 规则或 verdict 缺配置时：从 title + check_hint 切词，最多取 20 个
    hint_text = f"{rule.get('title') or ''} {rule.get('check_hint') or ''}"
    phrases = _split_chinese_phrases(hint_text)
    return phrases[:20]


# ── 匹配逻辑 ─────────────────────────────────────────────────────────


def _matches(text: str, keywords: Iterable[str]) -> bool:
    if not text or not keywords:
        return False
    for kw in keywords:
        if kw and kw in text:
            return True
    return False


def _build_item(rule: dict, *, status: str, matched_keyword: str | None) -> dict:
    basis = {
        _STATUS_HIT: _BASIS_HIT,
        _STATUS_NOTE: _BASIS_NOTE,
        _STATUS_CLEAR: _BASIS_CLEAR,
    }.get(status, _DEFAULT_BASIS)
    level = rule.get("legal_level") or "hint"
    # verdict 命中 → evidence_level=verdict；hint 命中 → evidence_level=hint
    # verdict 未命中 → 仍记 evidence_level=verdict（已核验通过）
    evidence_level = "verdict" if level == "verdict" else "hint"
    item = {
        "rule_id": rule.get("rule_id"),
        "title": rule.get("title"),
        "status": status,
        "evidence_level": evidence_level,
        "basis": basis,
        "checked": True,
        "source_url": rule.get("source_url"),
    }
    if matched_keyword is not None:
        item["matched_keyword"] = matched_keyword
    return item


# ── Skill ─────────────────────────────────────────────────────────────


class RulesScreenSkill(Skill):
    """法规规则筛查：按规则库关键词筛 tender/bid 文本，分级报告命中/提示/通过。"""

    name = "rules_screen"
    description = "法规规则筛查：按规则库关键词筛 bid/tender 文本，verdict 命中报条款、hint 命中出提示"
    category = "check"
    version = "1.0.0"
    triggers: ClassVar[list[str]] = ["法规筛查", "规则筛查", "关键词筛查", "条款筛查"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        params = ctx.parameters or {}
        tender_text: str = params.get("tender_text", "") or ""
        bid_text: str = params.get("bid_text", "") or ""

        if not tender_text and not bid_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        # 目标文本：以 bid 为主、tender 为辅拼接（规则库多数面向投标文件）
        target_text = (bid_text + "\n\n" + tender_text).strip()
        if not target_text:
            return SkillResult(success=False, error="招标文件和投标文件内容不能为空")

        # 规则包定位 + 加载
        rules_path = _resolve_rules_path()
        try:
            rules = load_rules(rules_path)
        except FileNotFoundError as e:
            return SkillResult(success=False, error=f"规则包文件不存在: {e}")
        except ValueError as e:
            return SkillResult(success=False, error=f"规则包加载失败: {e}")

        items: list[dict] = []
        hit_count = 0
        note_count = 0
        clear_count = 0

        for rule in rules:
            if not isinstance(rule, dict):
                continue
            keywords = _keywords_for_rule(rule)
            if not keywords:
                # 无关键词视为不可筛：当作 hint 的 note（提示"未配置关键词"）
                items.append({
                    "rule_id": rule.get("rule_id"),
                    "title": rule.get("title"),
                    "status": _STATUS_NOTE,
                    "evidence_level": "hint",
                    "basis": "规则未配置关键词 → 需人工判断",
                    "checked": False,
                    "source_url": rule.get("source_url"),
                })
                note_count += 1
                continue

            matched = next((kw for kw in keywords if kw and kw in target_text), None)
            level = rule.get("legal_level")
            if matched:
                if level == "verdict":
                    items.append(_build_item(rule, status=_STATUS_HIT, matched_keyword=matched))
                    hit_count += 1
                else:
                    items.append(_build_item(rule, status=_STATUS_NOTE, matched_keyword=matched))
                    note_count += 1
            else:
                # 未命中：verdict 出 clear（依据分级 checked=True）；
                # hint 未命中不计入汇总（提示性条款不产出结果）
                if level == "verdict":
                    items.append(_build_item(rule, status=_STATUS_CLEAR, matched_keyword=None))
                    clear_count += 1

        payload: dict = {
            "items": items,
            "summary": {
                "hit": hit_count,
                "note": note_count,
                "clear": clear_count,
                "total": len(items),
            },
            "rules_path": str(rules_path),
            "risk_level": "high" if hit_count > 0 else "low",
        }

        # 汇聚层证据分级（本 skill 已显式声明 evidence_level + basis，
        # 汇聚层尊重原值并追加 evidence_summary）
        enrich_evidence(payload, skill_name=self.name)
        return SkillResult(success=True, data=payload)
