"""Skill 引擎 — 基础契约层。

定义技能（Skill）的抽象基类，以及执行上下文 (SkillContext) 和执行
结果 (SkillResult) 两个数据类。所有业务侧技能都应当继承 ``Skill``
并实现 ``execute`` 方法。

本模块只描述契约，不涉及技能的发现、注册或加载；那些属于
``core.skill_engine.loader`` 与 ``core.skill_engine.registry``。
"""

from __future__ import annotations

import inspect
import logging
import traceback
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型检查
    import sqlalchemy.ext.asyncio

logger = logging.getLogger(__name__)

# 进度回调约定：callback(name: str, event: str, payload: dict)
# event 的取值见下方 _PROGRESS_EVENTS
ProgressCallback = Callable[[str, str, dict], Any]

# 进度事件的三种取值
_EVENT_STARTED = "started"
_EVENT_COMPLETED = "completed"
_EVENT_FAILED = "failed"

# 全部进度事件的取值集合
_PROGRESS_EVENTS = (_EVENT_STARTED, _EVENT_COMPLETED, _EVENT_FAILED)


# ------------------------------------------------------------------
# 异常渲染
# ------------------------------------------------------------------


def _failure_reason(failure: Exception) -> str:
    """异常的可读原因。"""
    return str(failure)


def _failure_stack(failure: Exception) -> str:
    """异常的格式化调用栈。"""
    lines = traceback.format_exception(failure)
    return "".join(lines)


def _failure_detail(failure: Exception) -> str:
    """把异常渲染成「原因 + 调用栈」两段文本。"""
    head = _failure_reason(failure)
    tail = _failure_stack(failure)
    return f"{head}\n{tail}"


# ------------------------------------------------------------------
# 进度回调
# ------------------------------------------------------------------


async def _await_if_awaitable(pending) -> None:
    """回调返回可等待对象时等它结束；同步回调直接返回。"""
    if inspect.isawaitable(pending):
        await pending


async def _invoke_listener(
    listener: ProgressCallback, skill_name: str, event: str, payload: dict
) -> None:
    """调用进度回调 —— 可以是同步函数，也可以返回可等待对象。"""
    pending = listener(skill_name, event, payload)
    await _await_if_awaitable(pending)


@dataclass
class SkillContext:
    """传给技能 ``execute`` 方法的执行上下文。

    字段：
        project_id: 当前项目 ID，可接受 ``UUID`` 或字符串。
        db: 可选的异步数据库会话；纯 LLM/规则类技能可传 ``None``。
        llm: LLM 网关实例，用于自然语言生成/抽取。
        parameters: 由路由或调度器传入的业务参数。
        knowledge_base: 可选的知识库检索器（如存在则技能可自行调用 ``retrieve``）。
        progress_callback: 可选的进度回调，用于向前端推送阶段事件。
    """

    project_id: UUID | str
    db: sqlalchemy.ext.asyncio.AsyncSession | None
    llm: Any  # LLM 网关实例；本仓库的技能均不调用大模型，保留以兼容契约
    parameters: dict = field(default_factory=dict)
    knowledge_base: Any = None
    progress_callback: ProgressCallback | None = None


@dataclass
class SkillResult:
    """技能一次执行的结果对象。

    字段：
        success: 是否成功；False 时 ``error`` 应给出可读原因。
        data: 技能产出的结构化数据，路由直接透传给上层调用方。
        tokens_consumed: 本次执行消耗的 LLM token 数（若无 LLM 调用则为 0）。
        sources: 引用来源列表（如检索到的知识条目、参考文件）。
        error: 失败原因，成功时为 ``None``。
        warnings: 非致命告警信息列表，供上层展示或审计。
    """

    success: bool
    data: dict = field(default_factory=dict)
    tokens_consumed: int = 0
    sources: list[dict] = field(default_factory=list)
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


class Skill(ABC):
    """技能基类。子类通过覆盖类属性 ``name`` 等元数据并在 ``execute``
    中实现具体业务逻辑来完成注册。"""

    # 子类元数据 —— 供 registry/loader 读取用于列表和匹配。
    name: str = ""
    description: str = ""
    category: str = ""
    version: str = "1.0.0"
    triggers: ClassVar[list[str]] = []

    @abstractmethod
    async def execute(self, ctx: SkillContext) -> SkillResult:
        """执行技能的核心逻辑。子类必须实现。"""

    # ------------------------------------------------------------------
    # 执行辅助
    # ------------------------------------------------------------------

    async def safe_execute(self, ctx: SkillContext) -> SkillResult:
        """在 ``execute`` 外加一层保护：推送进度事件、吞掉异常并转成
        ``SkillResult(success=False, ...)``。上游调用者通常通过
        ``safe_execute`` 而不是直接调用 ``execute``，这样可以保证
        一次执行永远返回结构化结果而不抛异常。"""
        listener = ctx.progress_callback
        await self._publish(listener, _EVENT_STARTED, {})

        try:
            outcome = await self.execute(ctx)
        except Exception as failure:  # noqa: BLE001
            return await self._abort(listener, failure)

        await self._publish(listener, _EVENT_COMPLETED, outcome.data)
        return outcome

    async def _publish(
        self, listener: ProgressCallback | None, event: str, payload: dict
    ) -> None:
        """把一个进度事件推给回调。

        没有回调时静默跳过；回调自身抛异常也不应影响主流程，仅记 debug 日志。
        """
        if listener is None:
            return
        try:
            await _invoke_listener(listener, self.name, event, payload)
        except Exception:
            logger.debug(
                "进度回调调用失败 name=%s event=%s",
                self.name,
                event,
                exc_info=True,
            )

    async def _abort(
        self, listener: ProgressCallback | None, failure: Exception
    ) -> SkillResult:
        """执行失败：补发 failed 事件，并交回失败结果。"""
        logger.exception("Skill %s 执行异常", self.name)
        await self._announce_failure(listener, failure)
        return self._failure_result(failure)

    async def _announce_failure(
        self, listener: ProgressCallback | None, failure: Exception
    ) -> None:
        """向监听方补发 failed 事件，载荷带上失败摘要。"""
        summary = _failure_reason(failure)
        await self._publish(listener, _EVENT_FAILED, {"error": summary})

    def _failure_result(self, failure: Exception) -> SkillResult:
        """把异常包成失败结果。"""
        detail = _failure_detail(failure)
        return SkillResult(success=False, error=detail)
