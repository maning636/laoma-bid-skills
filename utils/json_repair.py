"""LLM 输出 JSON 容错解析。

设计思路：把 LLM 常见输出的"脏"形态视作一条流水线，逐步尝试更激进的清洗/修复。
对外 API 与旧版 JsonRepairEngine 保持一致：`extract_json`、`try_parse`、
`repair_and_validate` 以及可选的 schema/validator 二次校验。

覆盖的容错场景：
- 推理模型的 <think>...</think> 段落
- ```json ... ``` 代码块包裹
- Markdown 转义反斜杠（如 `\\.`）
- 未配对的大括号/方括号（截断）
- 尾随逗号、无引号 key、控制字符
- 上述策略均失败时委托外部 chat 函数做修复
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from .exceptions import JsonRepairError

_LOG = logging.getLogger("core.llm_gateway.repair")

# 推理模型常见 <think> ... </think> 段落（含常见残缺闭合）
_THINK_BLOCK = re.compile(r"<think[\s\S]*?</think\s*>")
# ```json``` 或裸 ``` 代码围栏
_CODE_FENCE = re.compile(r"```(?:json)?\s*\n?([\s\S]*?)\n?\s*```")
# Markdown 特殊字符前的多余反斜杠
_BAD_ESCAPES = re.compile(r"\\([.#$&*+<>?@^`|~])")
# 控制字符（除已保留的空白）
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# 尾随逗号
_TRAILING_COMMA = re.compile(r",\s*([}\]])")
# 未加引号的 key：`{ key: ...` 或 `, key: ...`
_NAKED_KEY = re.compile(r"([{,]\s*)(\w+)(\s*:)")

# LLM 修复调用的通用提示词
_REPAIR_SYSTEM_PROMPT = (
    "你是 JSON 修复器，仅输出合法的 JSON 文本，不要输出解释、"
    "markdown 代码块或多余内容。若原始内容被截断，请补齐使其成为完整合法的 JSON。"
)
_SCHEMA_SYSTEM_PROMPT = (
    "你是 JSON 修复器。给定一段 JSON 和它对应的 Schema 校验错误，"
    "仅输出修复后符合 Schema 的 JSON，不要任何解释。"
)


def _safe_json_load(text: str) -> tuple[Any, str | None]:
    """把 json.loads 的异常归一化为 (None, 错误描述)。"""
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, str(exc)


def _strip_think(text: str) -> str:
    """剥离 <think>...</think> 内容。"""
    return _THINK_BLOCK.sub("", text)


def _take_code_block(text: str) -> str | None:
    """若文本内含 ```json``` 代码块，返回其内部内容。"""
    match = _CODE_FENCE.search(text)
    return match.group(1).strip() if match else None


def _balance_candidates(text: str) -> list[str]:
    """通过栈扫描提取文本中所有配对的 {...} 与 [...] 片段。"""
    hits: list[str] = []
    for opener, closer in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        for idx, ch in enumerate(text):
            if ch == opener:
                if depth == 0:
                    start = idx
                depth += 1
            elif ch == closer and depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    hits.append(text[start : idx + 1])
                    start = -1
    return hits


def _strip_bad_escapes(text: str) -> str:
    """去掉 Markdown 特殊字符前的多余反斜杠。"""
    return _BAD_ESCAPES.sub(r"\1", text)


def _strip_control_chars(text: str) -> str:
    return _CONTROL_CHARS.sub("", text)


def _strip_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA.sub(r"\1", text)


def _quote_naked_keys(text: str) -> str:
    return _NAKED_KEY.sub(r'\1"\2"\3', text)


def _quote_broken_string(text: str) -> str:
    """若结尾停在未闭合的字符串内，则尝试补一个引号。"""
    in_str = False
    escaped = False
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
    return text + '"' if in_str else text


def _count_unclosed(text: str) -> tuple[int, int]:
    return (
        max(text.count("{") - text.count("}"), 0),
        max(text.count("[") - text.count("]"), 0),
    )


def _complete_brackets(text: str) -> str:
    """按不同顺序补齐未闭合的 {} / []，取首个能通过 json.loads 的结果。"""
    open_brace, open_bracket = _count_unclosed(text)
    if not (open_brace or open_bracket):
        return text
    suffix_variants = [
        "}" * open_brace + "]" * open_bracket,
        "]" * open_bracket + "}" * open_brace,
        "".join("}" if i % 2 == 0 else "]" for i in range(open_brace + open_bracket)),
    ]
    for suffix in suffix_variants:
        candidate = text + suffix
        parsed, _ = _safe_json_load(candidate)
        if isinstance(parsed, (dict, list)):
            return candidate
    return text + suffix_variants[0]


def _truncate_repair(text: str) -> str:
    """综合处理被截断的 JSON：去尾逗号、无引号 key、补引号、补括号。"""
    cleaned = text.rstrip()
    cleaned = cleaned.removesuffix(",")
    cleaned = re.sub(r":\s*\"[^\"]*$", ": null", cleaned)
    cleaned = re.sub(r":\s*$", ": null", cleaned)
    return _complete_brackets(_quote_broken_string(cleaned))


def _candidate_variants(base: str) -> Iterable[str]:
    """给出对基础文本的一批"就地修复"变体，供解析器依次尝试。"""
    variants = [
        _strip_trailing_commas(base),
        _quote_naked_keys(base),
        _truncate_repair(base),
        _truncate_repair(_strip_trailing_commas(base)),
        _truncate_repair(_quote_naked_keys(base)),
    ]
    seen = set()
    for variant in variants:
        if variant not in seen:
            seen.add(variant)
            yield variant


class JsonRepairEngine:
    """多层 JSON 修复引擎。

    公开 API：
        - `extract_json(text)`        去掉 think/代码块外壳
        - `try_parse(text)`           (parsed, error_message) 二元组
        - `await repair_and_validate(...)` 主入口
    """

    # ----------------------- 对外 API -----------------------

    @staticmethod
    def extract_json(text: str) -> str:
        """剥掉推理标签和 markdown 代码块，返回候选 JSON 原文。"""
        if not text or not text.strip():
            return text
        stripped = _strip_think(text)
        block = _take_code_block(stripped)
        return (block if block is not None else stripped.strip())

    @staticmethod
    def try_parse(text: str) -> tuple[Any, str | None]:
        """基础解析；成功返回 (obj, None)，失败返回 (None, error)。"""
        return _safe_json_load(text)

    async def repair_and_validate(
        self,
        raw_text: str,
        schema: type | None = None,
        validator: Callable | None = None,
        repair_chat_fn: Callable | None = None,
        max_attempts: int = 2,
    ) -> Any:
        """主入口：提取 → 解析 → 多路修复 → LLM 修复 → Schema/自定义校验。"""
        body = self.extract_json(raw_text)

        # 1) 直接解析
        parsed, error = self.try_parse(body)
        if parsed is not None:
            return await self._finalize(parsed, schema, validator, repair_chat_fn, max_attempts)

        # 2) 多路候选 + 基础修复
        repaired = self._attempt_pipeline(body)
        if repaired is not None:
            return await self._finalize(repaired, schema, validator, repair_chat_fn, max_attempts)

        # 3) 委托 LLM 修复
        if repair_chat_fn is not None:
            llm_result = await self._delegate_to_llm(body, error, repair_chat_fn)
            if llm_result is not None:
                return await self._finalize(llm_result, schema, validator, repair_chat_fn, max_attempts)

        raise JsonRepairError(f"JSON修复失败: {error}")

    # ----------------------- 内部管线 -----------------------

    def _build_candidates(self, text: str) -> list[str]:
        """按优先级生成清洗后的候选字符串列表。"""
        ordered: list[str] = [text]

        def _push_unique(candidate: str) -> None:
            if candidate not in ordered:
                ordered.append(candidate)

        escaped = _strip_bad_escapes(text)
        _push_unique(escaped)

        balanced = _balance_candidates(text)
        for chunk in balanced:
            _push_unique(chunk)
            re_escaped = _strip_bad_escapes(chunk)
            if re_escaped != chunk:
                _push_unique(re_escaped)

        stripped_ctrl = _strip_control_chars(text)
        _push_unique(stripped_ctrl)

        no_trailing = _strip_trailing_commas(text)
        _push_unique(no_trailing)

        return ordered

    def _attempt_pipeline(self, text: str) -> Any | None:
        """遍历候选列表，先尝试直接解析，再尝试基础修复。"""
        for candidate in self._build_candidates(text):
            parsed, _ = self.try_parse(candidate)
            if parsed is not None:
                return parsed
            for variant in _candidate_variants(candidate):
                parsed, _ = self.try_parse(variant)
                if isinstance(parsed, (dict, list)):
                    return parsed
        return None

    async def _finalize(
        self,
        parsed: Any,
        schema: type | None,
        validator: Callable | None,
        repair_chat_fn: Callable | None,
        max_attempts: int,
    ) -> Any:
        """对解析结果做 schema 与自定义校验，必要时请求 LLM 二次修复。"""
        if isinstance(parsed, list):
            return parsed

        if schema is not None:
            try:
                instance = schema.model_validate(parsed)
                parsed = instance.model_dump()
            except Exception as exc:
                if repair_chat_fn is None or max_attempts <= 0:
                    raise JsonRepairError(f"Schema校验失败: {exc}") from exc
                prompt_payload = json.dumps(parsed, ensure_ascii=False)
                messages = [
                    {"role": "system", "content": _SCHEMA_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Schema错误: {exc}\n当前JSON: {prompt_payload}",
                    },
                ]
                retried = await repair_chat_fn(messages=messages, temperature=0.1)
                return await self.repair_and_validate(
                    raw_text=retried,
                    schema=schema,
                    validator=validator,
                    repair_chat_fn=repair_chat_fn,
                    max_attempts=max_attempts - 1,
                )

        if validator is not None:
            issue = validator(parsed)
            if issue:
                raise JsonRepairError(f"自定义校验失败: {issue}")

        return parsed

    async def _delegate_to_llm(
        self,
        broken: str,
        error: str,
        chat_fn: Callable,
    ) -> Any | None:
        """调用外部 chat 函数完成修复；异常时返回 None 交回主流程。"""
        messages = [
            {"role": "system", "content": _REPAIR_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"错误: {error}\n内容: {broken[:4000]}",
            },
        ]
        try:
            reply = await chat_fn(
                messages=messages,
                temperature=0.1,
                response_format={"type": "json_object"},
            )
            extracted = self.extract_json(reply)
            parsed, _ = self.try_parse(extracted)
            return parsed if isinstance(parsed, (dict, list)) else None
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("[JsonRepair] LLM 修复失败: %s", exc)
            return None
