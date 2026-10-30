"""单角色事件运行时：顺序队列、路由与持久化提交编排。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from core.events import AgentEvent
from core.router import EventRouter
from core.state import AgentState
from infrastructure.storage import SQLiteStorage, StateNotFoundError


class AgentRuntimeError(RuntimeError):
    """运行时生命周期或事件接收错误。"""


class RuntimeNotRunningError(AgentRuntimeError):
    """运行时尚未启动或已经停止。"""


class RuntimeStoppingError(AgentRuntimeError):
    """运行时正在停止，不能启动或接收新事件。"""


class RuntimeQueueFullError(AgentRuntimeError):
    """排队事件已达到上限，本次事件尚未接收。"""


@dataclass
class _QueuedEvent:
    event: AgentEvent
    future: asyncio.Future[AgentState]


def _observe_task(task: asyncio.Task) -> None:
    """读取后台任务异常，避免停止等待被取消时出现未读取异常提示。"""
    if not task.cancelled():
        task.exception()


class AgentRuntime:
    """一个角色使用一个工作任务；数据库连接由调用方打开并管理。

    submit 等待该事件处理结果，多个调用方可并发提交，内部按入队顺序处理。
    已接收事件不会因调用方取消等待而撤回。停止时停止接收并排空已接收事件。
    队列仅在内存，尚不提供进程异常退出后的待处理队列恢复或自动失败重试。
    """

    def __init__(
        self,
        agent_id: str,
        storage: SQLiteStorage,
        router: EventRouter,
        *,
        max_pending: int = 100,
        clock: Callable[[], datetime] | None = None,
    ):
        if not isinstance(agent_id, str):
            raise ValueError("agent_id 必须是字符串")
        AgentState.validate_agent_id(agent_id)
        if type(max_pending) is not int or max_pending < 1:
            raise ValueError("max_pending 必须是正整数")
        if clock is not None and not callable(clock):
            raise ValueError("clock 必须是返回带时区 datetime 的可调用对象")
        self.agent_id = agent_id
        self._storage = storage
        self._router = router
        self._max_pending = max_pending
        self._clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self._queue: asyncio.Queue[_QueuedEvent | None] = asyncio.Queue(maxsize=max_pending)
        self._lifecycle_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._accepting = False

    @property
    def is_running(self) -> bool:
        """当前是否接受新事件。"""
        return self._accepting and self._worker is not None and not self._worker.done()

    async def __aenter__(self) -> AgentRuntime:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.stop()

    async def start(self) -> None:
        """确认已有角色状态并启动工作任务；不重新初始化角色。"""
        async with self._lifecycle_lock:
            if self._stop_task is not None and not self._stop_task.done():
                raise RuntimeStoppingError("请等待本次停止完成后再启动")
            if self.is_running:
                return
            state = await self._storage.get_state(self.agent_id)
            if state is None:
                raise StateNotFoundError("请先创建角色初始状态")
            self._queue = asyncio.Queue(maxsize=self._max_pending)
            self._stop_task = None
            self._accepting = True
            self._worker = asyncio.create_task(self._run(), name=f"persona:{self.agent_id}")
            self._worker.add_done_callback(_observe_task)

    async def stop(self) -> None:
        """停止接收新事件，等待已接收事件处理完，不关闭外部数据库连接。"""
        if asyncio.current_task() is self._worker:
            raise AgentRuntimeError("处理函数不能等待自身运行时停止")
        async with self._lifecycle_lock:
            if self._worker is None:
                return
            if self._stop_task is None:
                self._accepting = False
                self._stop_task = asyncio.create_task(self._finish_stop(self._worker))
                self._stop_task.add_done_callback(_observe_task)
            stop_task = self._stop_task
        # 调用方取消等待时，后台仍继续排空队列和完成停止。
        await asyncio.shield(stop_task)

    async def _finish_stop(self, worker: asyncio.Task[None]) -> None:
        try:
            if not worker.done():
                await self._queue.put(None)
            await worker
        finally:
            async with self._lifecycle_lock:
                if self._worker is worker:
                    self._worker = None

    async def submit(self, event: AgentEvent) -> AgentState:
        """接收事件并等待其结果；队列满时立即报错，未接收的事件可稍后重试。"""
        if asyncio.current_task() is self._worker:
            raise AgentRuntimeError("处理函数不能等待同一运行时的新事件")
        event = AgentEvent.model_validate(event.model_dump())
        async with self._lifecycle_lock:
            if self._stop_task is not None and not self._stop_task.done():
                raise RuntimeStoppingError("运行时正在停止，不能接收事件")
            if not self.is_running:
                raise RuntimeNotRunningError("请先启动运行时")
            future = asyncio.get_running_loop().create_future()
            try:
                self._queue.put_nowait(_QueuedEvent(event, future))
            except asyncio.QueueFull:
                raise RuntimeQueueFullError("等待队列已满，本次事件未接收") from None
        return await future

    async def _run(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                try:
                    if item is None:
                        return
                    try:
                        state = await self._process_event(item.event)
                    except asyncio.CancelledError:
                        if not item.future.done():
                            item.future.cancel()
                        raise
                    except Exception as error:
                        if not item.future.done():
                            # 不把仍在运行的 _run 帧交给另一个任务。
                            # unittest 等调用方可能清理异常栈，关闭栈中的暂停协程。
                            # 当前首帧是本轮 await _process_event，去掉它并保留业务栈。
                            traceback = error.__traceback__
                            item.future.set_exception(
                                error.with_traceback(traceback.tb_next if traceback else None)
                            )
                            del traceback
                    else:
                        if not item.future.done():
                            item.future.set_result(state)
                finally:
                    self._queue.task_done()
        finally:
            self._accepting = False
            # 工作任务异常结束时，不能让排队调用方永远等待。
            while not self._queue.empty():
                pending = self._queue.get_nowait()
                if pending is not None and not pending.future.done():
                    pending.future.cancel()
                self._queue.task_done()

    async def _process_event(self, event: AgentEvent) -> AgentState:
        await self._storage.record_event(event)
        committed = await self._storage.get_committed_proposal(self.agent_id, event.event_id)
        if committed is not None:
            # 复用已提交的原提案，不基于新状态重新执行规则或生成不同内容。
            return await self._storage.commit_proposal(committed, settled_at=self._clock())
        state = await self._storage.get_state(self.agent_id)
        if state is None:
            raise StateNotFoundError("角色状态不存在")
        proposal = await self._router.dispatch(event, state)
        if proposal is None:
            return state
        return await self._storage.commit_proposal(proposal, settled_at=self._clock())
