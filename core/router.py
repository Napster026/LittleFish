"""按具体事件类型分发到异步处理函数，返回经校验的状态变更提案。"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable

from core.events import AgentEvent
from core.settlement import StateChangeProposal
from core.state import AgentState


EventHandler = Callable[[AgentEvent, AgentState], Awaitable[StateChangeProposal | None]]


class RouterError(RuntimeError):
    """事件路由的注册或处理结果不符合约定。"""


class DuplicateHandlerError(RouterError):
    """同一具体事件类型不能重复注册处理函数。"""


class InvalidHandlerError(RouterError):
    """注册对象不可调用，或调用后没有返回可等待对象。"""


class InvalidHandlerResultError(RouterError):
    """处理结果类型不合法，或提案关联的事件、角色、版本不匹配。"""


class EventRouter:
    """一个具体事件类型对应一个异步处理函数。

    处理函数接收事件和状态的独立数据副本，返回汇总状态提案或 None。
    没有处理函数时返回 None。注册应在运行前完成，当前不支持通配路由、
    多处理函数广播或热替换。路由器不调度任务、不决定全局行为，也不写库。
    """

    def __init__(self):
        self._handlers: dict[str, EventHandler] = {}

    def register(self, event_type: str, handler: EventHandler) -> None:
        """注册精确事件类型，拒绝非法名称、不可调用对象和重复注册。"""
        if not isinstance(event_type, str):
            raise ValueError("事件类型必须是字符串")
        # 复用事件模型的命名规则，避免路由与事件使用不同的协议。
        AgentEvent.validate_event_type(event_type)
        if not callable(handler):
            raise InvalidHandlerError("处理函数必须可调用")
        if event_type in self._handlers:
            raise DuplicateHandlerError(f"事件类型 {event_type} 已经注册处理函数")
        self._handlers[event_type] = handler

    async def dispatch(self, event: AgentEvent, state: AgentState) -> StateChangeProposal | None:
        """按事件类型调用处理函数，并验证提案与输入数据的关联。

        处理函数应只计算提案，禁止数据库写入、消息发送等外部副作用。
        提案 ID 由处理函数按稳定策略分配，重试不能随意换 ID 或改内容。
        处理函数的异常和取消原样向调用方传播，不伪装成正常的 None。
        """
        # 重新校验并复制输入，避免处理函数改动 payload 的嵌套容器时污染原事件。
        event = AgentEvent.model_validate(event.model_dump())
        state = AgentState.model_validate(state.model_dump())
        handler = self._handlers.get(event.event_type)
        if handler is None:
            return None

        pending = handler(event, state)
        if not inspect.isawaitable(pending):
            raise InvalidHandlerError("处理函数必须返回可等待对象，请使用 async def")
        result = await pending
        if result is None:
            return None
        if not isinstance(result, StateChangeProposal):
            raise InvalidHandlerResultError("处理函数只能返回 StateChangeProposal 或 None")

        result = StateChangeProposal.model_validate(result.model_dump())
        if result.event_id != event.event_id:
            raise InvalidHandlerResultError("提案依据的事件与本次事件不一致")
        if result.agent_id != state.agent_id:
            raise InvalidHandlerResultError("提案目标角色与本次状态不一致")
        if result.expected_version != state.version:
            raise InvalidHandlerResultError("提案预期版本与本次读取的状态版本不一致")
        return result
