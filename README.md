# laoma-bid-skills

**给 AI Agent 用的投标文档处理技能集** —— 8 个技能，**确定性执行，不调用大模型**。

做招投标自动化时，有一堆活是「规则明确、但人工做又慢又容易漏」的：查报价算术、清文档属性、核格式、生成自检清单。这些活**不需要 AI 判断**，需要的是**准确、可重复、零成本**。

这个库把它们做成 Agent 可以直接调用的技能：Agent 决定「查什么」，技能负责「查得准」。

[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![CI](https://github.com/maning636/laoma-bid-skills/actions/workflows/ci.yml/badge.svg)](https://github.com/maning636/laoma-bid-skills/actions/workflows/ci.yml)
[![LLM](https://img.shields.io/badge/LLM-0%20calls-success)](README.md)

---

## 先说清楚：什么叫「不调用大模型」

**这一节最重要，因为它决定了你该在什么时候用它、什么时候不该用。**

### 大模型擅长什么、不擅长什么

| 任务类型 | 例子 | 谁来干 |
|---|---|---|
| **需要理解和判断** | 「这份招标文件对投标人资质有什么隐含要求？」 | ✅ 大模型 |
| **需要创造** | 「帮我写一段技术方案，800 字」 | ✅ 大模型 |
| **规则明确、结果唯一** | 「单价 × 数量 是不是等于合价？」 | ❌ 大模型（会算错、会编、每次都不同） |
| **需要穷尽扫描** | 「这份 200 页文档里，哪几处字体不是宋体？」 | ❌ 大模型（看不全、会漏） |
| **必须可复现** | 「同一份文件，昨天查是 3 个问题，今天还是 3 个」 | ❌ 大模型（每次结果都变） |

**本库的 8 个技能全部属于后三类。**

### 具体差异（同样查一份报价表）

| | 用大模型 | 用本库技能 |
|---|---|---|
| 算术准确性 | 可能算错（LLM 不擅长精确计算） | **精确**，`3.2×10=32.00` 就是 32.00 |
| 覆盖率 | 看到哪算哪，长文档会漏 | **穷尽扫描**，逐行逐段 |
| 同一文件重复跑 | 结果可能不同 | **结果完全一致** |
| 成本 | 每次调用都花钱 | **零成本** |
| 速度 | 几秒到几十秒 | **毫秒级** |
| 断网 | 不可用 | **可完全离线** |
| 数据安全 | 文档内容要发到模型服务商 | **数据不出本机** |

### 所以它适合什么场景

**✅ 适合：Agent 的「工具层」**

Agent（比如 Claude、GPT、Kimi 这类）负责理解用户意图、决定查什么、把结果解释给用户。它调用本库技能去干那些「必须准确」的活：

```
用户：帮我看看这份投标文件有没有问题
  ↓
Agent 理解意图，决定分几步查
  ↓
调用 pricing_check    → 查出 2 处算术错（精确，不含糊）
调用 anon_bid_clean   → 查出 3 处公司名残留（穷尽，不漏）
调用 docx_format      → 查出 1218 处格式问题（逐段扫）
  ↓
Agent 拿到结构化结果，组织成人类能读的报告
```

**Agent 不需要自己算算术、自己数段落** —— 那些交给技能，又快又准又免费。

**❌ 不适合：让它「理解」或「创作」**

- 不能让 `pricing_check` 判断「这个报价策略合不合理」—— 那是判断，要 Agent 来做
- 不能让 `docx_format` 帮你「改写这段方案」—— 那是创作，要 Agent 来做

**一句话：技能负责「查得准」，Agent 负责「想明白」。**

---

## 为什么值得一看

| 常见痛点 | 本库的解法 |
|---|---|
| Word 排版不可控：字体、字号、缩进、行距、页码，人工抄格式必出错 | `docx_format` 四种模式（排版 / 格式检查 / 模板对比 / XML 美化），能报告「哪一段字体不对」 |
| 投标文件带出公司名、作者、修订记录，暗标场景直接废标 | `anon_bid_clean` 扫描文档属性、页眉页脚 Logo、正文公司名、docProps |
| 报价表算术错误：单价×数量≠合价、大写小写不一致、超限价 | `pricing_check` 四类规则一次跑完，报出具体行号 |
| 检查结果散在十几个地方，没法交给人看 | `check_report_export` 渲染成 Markdown / HTML / 文本报告 |
| 自检靠人列清单，每次重来 | `selfcheck_list` 按检查结果自动生成清单，标出哪些还没过 |
| 规则散在代码里，改一条要发版 | `rules_screen` 规则包 JSON 驱动，改规则不改代码 |
| 风险靠感觉，说不清哪一维高风险 | `risk_score` 六维加权评分 |
| 大模型返回的 JSON 十次有一次是坏的 | `utils/json_repair` 多候选修复 + 逐级降级，不抛异常 |

## 8 个技能

全部**确定性执行**：同一输入必得同一输出，不调大模型，不产生 API 费用。

| 技能 | 干什么 | 为什么不需要 AI | 依赖 |
|---|---|---|---|
| `docx_format` | Word 排版、格式检查、模板对比 | 格式是「字体=宋体、字号=小四」这种硬规则，逐段比对即可 | python-docx |
| `anon_bid_clean` | 清文档属性、页眉 Logo、正文公司名 | 「文档里有没有出现这个公司名」是字符串匹配，不需要理解 | python-docx |
| `rules_screen` | 按规则包筛内容 | 规则是人写好的 JSON，命中即报 | — |
| `check_report_export` | 把检查结果渲染成报告 | 模板填充，纯格式化 | — |
| `risk_score` | 六维加权评分 | 加权求和，权重是人定的 | — |
| `pricing_check` | 报价算术核查 | 算术就是算术，`3.2×10` 只有唯一答案 | — |
| `selfcheck_list` | 生成自检清单 | 按模板 + 已有检查结果点亮，逻辑固定 | — |
| `self_score` | 自评分估算 | 按内容长度分档打分，规则透明 | — |

另有三个基础包：`skill_engine/`（技能引擎）、`rules_engine/`（规则包读写）、`pathkit/`（路径解析）。

每个技能的完整契约见其目录下的 `SKILL.md`。

## 和 Agent 怎么配合

### 典型架构

```
┌─────────────────────────────────────┐
│  Agent（Claude / GPT / Kimi / 自研）  │  ← 理解意图、决定查什么、解释结果
└──────────────┬──────────────────────┘
               │ 调用（工具调用 / function calling）
┌──────────────▼──────────────────────┐
│  laoma-bid-skills（本库）             │  ← 确定性执行、结构化返回
│  pricing_check / docx_format / ...   │
└──────────────┬──────────────────────┘
               │ SkillResult(success, data, error)
               └→ 结构化 JSON 回到 Agent
```

### 一个完整对话的例子

**用户问：** 「这份投标文件能提交吗？帮我全面检查一下。」

**Agent 的工作流：**

| 步骤 | Agent 做什么 | 技能做什么 |
|---|---|---|
| 1 | 判断「全面检查」需要查哪些维度 | — |
| 2 | 调用 `docx_format`（mode=check） | 逐段扫格式，返回「1218 处问题，合规率 43.3%」 |
| 3 | 调用 `pricing_check` | 逐行验算，返回「第 2 项 3.2×10=32≠35」 |
| 4 | 调用 `anon_bid_clean` | 扫元数据，返回「作者字段残留真实姓名」 |
| 5 | 调用 `selfcheck_list` | 汇总成清单，返回「12 项中 9 项已过」 |
| 6 | **拿到四份结构化结果，组织成人话** | — |

**Agent 不需要自己算算术、自己数段落、自己扫元数据** —— 那些交给技能。

### 为什么这样分工更好

| | 全交给 Agent | Agent + 本库技能 |
|---|---|---|
| 算术准确率 | 大模型会算错 | 100% 准确 |
| 200 页文档覆盖率 | 看不全，会漏 | 逐段扫，不漏 |
| 重复跑一致性 | 每次结果不同 | 完全一致 |
| Token 成本 | 文档全文进上下文，很贵 | 技能只回结论，很便宜 |
| 数据安全 | 文档要发到模型服务商 | 技能在本机跑，数据不出门 |
| 断网 | 不可用 | 技能仍可用 |

### 什么时候**不该**用本库

- 想让它「理解」招标文件的隐含要求 → 用 Agent
- 想让它「写」技术方案 → 用 Agent
- 想让它「判断」报价策略合不合理 → 用 Agent

**本库只做「查得准」的事，不做「想明白」的事。**

## 安装

```bash
pip install git+https://github.com/maning636/laoma-bid-skills.git
```

第三方依赖只有 `python-docx` / `lxml` / `PyYAML`（后两个是 python-docx 的依赖）。

## 快速开始

### 报价算术核查（零依赖，最容易试）

```python
import asyncio
from skill_engine.base import SkillContext
from skills.pricing_check import PricingCheckSkill

bid_text = """
投标总价：98.5万
总金额：玖拾捌万伍仟元（985000）
1. 服务器 12.50 × 8 = 100.00
2. 交换机 3.20 × 10 = 35.00
"""

ctx = SkillContext(
    project_id="demo", db=None, llm=None,
    parameters={"bid_text": bid_text, "max_price": 100.0},
)
result = asyncio.run(PricingCheckSkill().execute(ctx))

print(result.data["risk_level"])     # high
for c in result.data["checks"]:
    print(c["status"], c["check_type"], c["detail"])
```

输出（第 2 项算错 3 万，报价又贴着限价 —— 两处都会被人工漏掉）：

```
pass    price_limit      投标总价98.5万未超过限价100.0万
pass    upper_lower_case 大写:玖拾捌万伍仟元 小写:985000元 (需人工确认一致)
fail    arithmetic       第2项: 3.2×10.0=32.00≠35.0
fail    safe_range       报价为限价98.5%，超出98%安全带上限
```

### Word 排版检查

```python
import asyncio
from skill_engine.base import SkillContext
from skills.docx_format import DocxFormatSkill

ctx = SkillContext(
    project_id="demo", db=None, llm=None,
    parameters={"file_path": "你的标书.docx", "mode": "check"},
)
result = asyncio.run(DocxFormatSkill().execute(ctx))

print(result.data["total_issues"])        # 1218（真实标书实测：739 段 / 2149 run）
print(result.data["compliance_rate"])     # 43.3
```

`mode` 支持 `format`（排版）/ `check`（检查）/ `diff`（与模板对比）/ `beautify`（XML 美化）。

### 暗标脱敏

```python
import asyncio

from skill_engine.base import SkillContext
from skills.anon_bid_clean import AnonBidCleanSkill

ctx = SkillContext(
    project_id="demo", db=None, llm=None,
    parameters={
        "file_path": "投标文件.docx",
        "company_names": ["某某科技有限公司", "某某科技"],
        "region": "beijing",          # national / beijing / guangdong / jiangsu
    },
)
result = asyncio.run(AnonBidCleanSkill().execute(ctx))
print(result.data["passed"])          # 有 critical 项时为 False
```

### 修复坏 JSON（大模型输出兜底）

```python
import asyncio
from utils.json_repair import JsonRepairEngine

engine = JsonRepairEngine()

# 剥掉 <think> 标签与 markdown 代码块
print(engine.extract_json('```json
{"a": 1}
```'))   # {"a": 1}

# 修复常见破损（尾逗号 / 裸键 / 未闭合括号），失败不抛异常
fixed = asyncio.run(engine.repair_and_validate('{"a": 1, "b": [1,2,], }'))
print(fixed)                                             # {'a': 1, 'b': [1, 2]}
```

## 接入 Agent（可复制的最小示例）

下面是「Agent 决定查什么 → 技能执行 → 结果回 Agent」的完整闭环，用 OpenAI 风格的工具调用举例。
换成 Claude / Kimi / 自研框架，结构一样，只是工具声明格式不同。

```python
import asyncio
import json

from skill_engine.base import SkillContext
from skills import PricingCheckSkill, AnonBidCleanSkill

# ── 1. 把技能声明成 Agent 可调用的工具 ──────────────────
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "pricing_check",
            "description": (
                "核查投标报价的算术正确性：是否超最高限价、大写小写金额是否都有、"
                "逐行单价×数量是否等于合价、报价是否落在合理区间。"
                "返回结构化问题清单，不调用大模型。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "bid_text": {"type": "string", "description": "投标文件正文"},
                    "max_price": {"type": "number", "description": "最高限价（万元）"},
                },
                "required": ["bid_text"],
            },
        },
    },
    # anon_bid_clean / docx_format / ... 同理，每个技能一个工具声明
]

# ── 2. 技能分发器：Agent 说调哪个，就执行哪个 ────────────
SKILL_MAP = {
    "pricing_check": PricingCheckSkill,
    "anon_bid_clean": AnonBidCleanSkill,
}


async def run_skill(name: str, arguments: dict) -> dict:
    """执行技能，把 SkillResult 转成 Agent 能读的 JSON。"""
    skill_cls = SKILL_MAP.get(name)
    if skill_cls is None:
        return {"success": False, "error": f"未知技能: {name}"}

    ctx = SkillContext(
        project_id="agent-call",
        db=None,
        llm=None,          # 关键：技能不需要 LLM
        parameters=arguments,
    )
    result = await skill_cls().execute(ctx)

    if not result.success:
        return {"success": False, "error": result.error}
    return {"success": True, "data": result.data}


# ── 3. Agent 循环（伪代码，接你自己的 LLM 客户端）──────────
async def agent_loop(user_message: str, bid_text: str):
    messages = [
        {"role": "system", "content": "你是投标文件检查助手。需要精确检查时调用工具，不要自己算。"},
        {"role": "user", "content": f"{user_message}

投标文件内容：
{bid_text}"},
    ]

    # 第一轮：Agent 决定调哪个工具
    # resp = await your_llm_client.chat(messages=messages, tools=TOOLS)
    # 下面用「Agent 决定调 pricing_check」模拟这个决策：
    tool_call = {"name": "pricing_check",
                 "arguments": {"bid_text": bid_text, "max_price": 100.0}}

    # 执行技能（毫秒级、零成本、结果确定）
    tool_result = await run_skill(tool_call["name"], tool_call["arguments"])

    # 把结果回给 Agent，让它组织成人话
    messages.append({
        "role": "tool",
        "name": tool_call["name"],
        "content": json.dumps(tool_result, ensure_ascii=False),
    })

    # 第二轮：Agent 基于结构化结果生成最终答复
    # final = await your_llm_client.chat(messages=messages)
    # return final.content
    return tool_result   # 这里直接返回结构化结果，方便你看清数据形状


if __name__ == "__main__":
    out = asyncio.run(agent_loop(
        "这份报价能提交吗？",
        "投标总价：98.5万
1. 服务器 12.50 × 8 = 100.00
2. 交换机 3.20 × 10 = 35.00",
    ))
    print(json.dumps(out, ensure_ascii=False, indent=2))
```

**关键点**：

1. **工具描述要写清「什么时候用」**，不只是「能干什么」—— Agent 靠这段文字决定调不调
2. **`llm=None`** —— 技能内部完全不碰 LLM，你传 `None` 它也能跑
3. **返回结构化 JSON** —— Agent 拿到的是 `{success, data}`，不是一段需要再解析的文字

## 设计：为什么用「技能」而不是函数

每个技能都是 `Skill` 子类，实现统一的 `execute(ctx) -> SkillResult`：

```python
class Skill(ABC):
    name: str
    description: str
    category: str
    version: str
    triggers: list[str]
    required_plan: str = "free"

    @abstractmethod
    async def execute(self, ctx: SkillContext) -> SkillResult: ...
```

好处：

- **统一错误处理**：失败返回 `SkillResult(success=False, error=...)`，不抛异常
- **统一进度回调**：长任务可通过 `ctx.progress_callback` 推进度
- **可组合**：一个技能的输出（`SkillResult.data`）可以直接喂给下一个技能
- **易测试**：不依赖 Web 框架、不依赖数据库（`db` / `llm` 都可为 `None`）

## 目录结构

```
skill_engine/          # 技能引擎（基类 / 上下文 / 结果 / 注册表 / 加载器）
rules_engine/          # 规则包读取与过滤（纯标准库）
pathkit/               # 路径解析（app_root / data_root）
skills/                # 8 个技能，每个含 SKILL.md
  docx_format/
  anon_bid_clean/
  rules_screen/
  check_report_export/
  risk_score/
  pricing_check/
  selfcheck_list/
  self_score/
utils/json_repair.py   # 坏 JSON 修复
examples/              # 最小可跑示例
tests/                 # 81 条测试
```

## 测试

```bash
pip install -e ".[dev]"
pytest
```

81 条，全部不依赖网络、不依赖真实标书文件、不调大模型。

## 边界（诚实说明）

### 能力边界

- **不做「理解」和「创作」**：解读招标文件、写技术方案、判断报价策略合不合理 —— 这些要 Agent 来做，本库不做
- **不含任何提示词**：所有技能都是确定性规则，没有提示词
- **不含私有知识库**：检索增强能力在主产品里，不在开源部分
- **`docx_format` 只处理 .docx**：不处理 .doc / PDF / OFD
- **`pricing_check` 只查算术与格式**：不判断报价是否「合理」，不预测中标概率

### 已知局限（会影响结果的）

| 局限 | 表现 | 我们怎么处理 |
|---|---|---|
| 正则只认常见排版 | 特殊格式的报价行可能没被匹配到 | 对应检查项返回 `warning` 而非静默通过 —— **宁可提示你人工确认，也不假装查过了** |
| `docx_format` 依赖 python-docx | 极旧或损坏的 .docx 可能打不开 | 返回 `success=False` + 具体错误，不抛异常 |
| `anon_bid_clean` 的公司名要你提供 | 不传 `company_names` 就跳过正文扫描 | 返回 `warning` 明确告知「跳过」，不静默 |
| `risk_score` 的权重是人定的 | 权重不合理则分数无意义 | 权重是公开常量，可自行调整后重算 |

**核心原则：查不出来就说「查不出来」，绝不静默通过。** 这在废标场景里比「误报」重要得多 —— 误报你还能复核，漏报你直接废标。

## 许可

**AGPL-3.0**。全文见 `LICENSE`。

本库的 `skill_engine/`、`skills/docx_format/` 与 `utils/json_repair.py` **派生自 AGPL-3.0 上游项目** [BidMaster-Pro](https://github.com/guangshu100/BidMaster-Pro)（经由「老马标书贤内助」主仓），因此本仓库整体必须保持 AGPL-3.0，**不得改为 MIT 或其他更宽松许可**。上游归属与实测相似度见 `NOTICE`。

如果你**分发**本库（含修改版、含打包进你的产品），AGPL-3.0 要求你保留归属声明并向接收者提供完整对应源代码；如果你**通过网络提供服务**，第 13 条要求你向服务使用者提供获取对应源代码的方式。仅在组织内部使用不触发上述义务。

## 相关

- 单技能精简版（零依赖，只含 `pricing_check`）：[laoma-bid-pricing-check](https://github.com/maning636/laoma-bid-pricing-check)
