"""活动生命周期调用编排与执行器结果边界检查。"""

from __future__ import annotations

import inspect

from .executor import (
    ActivityContext, ActivityExecutionError, ActivityExecutionResult,
    ExecutorCapabilities, InvalidActivityStatusError,
)
from .models import ActivityInstance, ActivityRecord, ActivityStatus, ExecutionMode
from .registry import ExecutorRegistry
from .feedback import STAGE_EVENT_TYPE, parse_feedback_event, parse_activity_feedback
from ..core.events import EventCategory, EventProvenance


class UnsupportedExecutorCapabilityError(ActivityExecutionError):
    """执行器没有声明本次操作需要的可选能力。"""


class InvalidExecutorResultError(ActivityExecutionError):
    """执行器的返回类型、身份、修订或生命周期不符合约定。"""


_TERMINAL = {ActivityStatus.COMPLETED, ActivityStatus.FAILED, ActivityStatus.CANCELLED}
_INPUT_STATUS = {
    "start": ActivityStatus.PENDING,
    "step": ActivityStatus.RUNNING,
    "pause": ActivityStatus.RUNNING,
    "resume": ActivityStatus.PAUSED,
}
_OUTPUT_STATUS = {
    "start": {ActivityStatus.RUNNING} | _TERMINAL,
    "step": {ActivityStatus.RUNNING} | _TERMINAL,
    "pause": {ActivityStatus.PAUSED} | _TERMINAL,
    "resume": {ActivityStatus.RUNNING} | _TERMINAL,
    "stop": _TERMINAL,
}
_CAPABILITY = {
    "pause": "supports_pause", "resume": "supports_resume", "snapshot": "supports_snapshot",
}


class ActivityManager:
    """统一选择、调用执行器并核对候选结果，不缓存或持久化实例。

    调用方必须提供最新上下文，本层不负责活动选择、任务栈、自动调度或收益
    提交。真实操作的权限与资源控制仍待接入；当前 ActivityService 只自动
    驱动模拟模式。这里的返回结果检查不能撤销已经发生的外部操作。
    """

    def __init__(self, registry: ExecutorRegistry):
        self._registry = registry

    async def _invoke(
        self, operation: str, context: ActivityContext
    ) -> ActivityExecutionResult | ActivityInstance:
        context = ActivityContext.model_validate(context.model_dump())
        expected = _INPUT_STATUS.get(operation)
        if expected is not None and context.instance.status != expected:
            raise InvalidActivityStatusError(f"{operation} 要求活动状态为 {expected.value}")
        executor = self._registry.resolve(context.definition, context.instance.mode)
        capabilities = ExecutorCapabilities.model_validate(executor.capabilities.model_dump())
        capability = _CAPABILITY.get(operation)
        if capability is not None and not getattr(capabilities, capability):
            raise UnsupportedExecutorCapabilityError(f"执行器未声明支持 {operation}")

        pending = getattr(executor, operation)(context)
        if not inspect.isawaitable(pending):
            raise InvalidExecutorResultError("执行器方法必须返回可等待对象")
        result = await pending
        if operation == "snapshot":
            if not isinstance(result, ActivityInstance):
                raise InvalidExecutorResultError("snapshot 必须返回 ActivityInstance")
            snapshot = ActivityInstance.model_validate(result.model_dump())
            if snapshot != context.instance:
                raise InvalidExecutorResultError("snapshot 不能改变当前活动实例")
            return snapshot

        if not isinstance(result, ActivityExecutionResult):
            raise InvalidExecutorResultError("执行器必须返回 ActivityExecutionResult")
        result = ActivityExecutionResult.model_validate(result.model_dump())
        self.validate_result(operation, context, result)
        return result

    @staticmethod
    def validate_result(
        operation: str, context: ActivityContext, result: ActivityExecutionResult
    ) -> None:
        """检查已通过模型校验的候选结果，供管理器与活动持久化规则共同使用。"""
        if operation not in {"start", "step", "observe", "pause", "resume", "stop"}:
            raise InvalidExecutorResultError("该操作不支持提交活动结果")
        expected = _INPUT_STATUS.get(operation)
        if expected is not None and context.instance.status != expected:
            raise InvalidActivityStatusError(f"{operation} 要求活动状态为 {expected.value}")
        previous, current = context.instance, result.instance
        for name in ("activity_id", "definition_id", "agent_id", "mode", "created_at"):
            if getattr(previous, name) != getattr(current, name):
                raise InvalidExecutorResultError(f"执行器不能改变活动身份字段 {name}")
        if current != previous:
            if previous.status in _TERMINAL:
                raise InvalidExecutorResultError("终态活动不能再改变实例状态")
            if current.revision != previous.revision + 1:
                raise InvalidExecutorResultError("状态变化必须使 revision 恰好加 1")
            if current.updated_at < previous.updated_at:
                raise InvalidExecutorResultError("活动更新时间不能倒退")
        allowed = _OUTPUT_STATUS.get(operation)
        if allowed is not None and current.status not in allowed:
            raise InvalidExecutorResultError(f"执行器返回的状态不符合 {operation} 操作")
        if current != previous and not result.events:
            raise InvalidExecutorResultError("活动状态变化必须提供反馈事件")

        for event in result.events:
            if event.event_type == STAGE_EVENT_TYPE:
                if current.status not in {ActivityStatus.RUNNING, ActivityStatus.PAUSED}:
                    raise InvalidExecutorResultError("阶段反馈不能把整项活动标记为终态")
                if event.timestamp > context.now:
                    raise InvalidExecutorResultError("阶段反馈不能来自未来")
                parse_feedback_event(ActivityRecord(definition=context.definition, instance=current), event)
            if event.event_type == "ACTIVITY.completed" and current.status != ActivityStatus.COMPLETED:
                raise InvalidExecutorResultError("完成事件必须有整项活动 completed 状态")
            if event.event_type == "ACTIVITY.failed" and current.status != ActivityStatus.FAILED:
                raise InvalidExecutorResultError("失败事件必须有整项活动 failed 状态，子任务失败使用阶段反馈")
            if event.event_type in {"ACTIVITY.completed", "ACTIVITY.failed"} and "feedback" in event.payload:
                parse_activity_feedback(ActivityRecord(definition=context.definition, instance=current), event)
            payload = event.payload
            if payload.get("definition_id", current.definition_id) != current.definition_id:
                raise InvalidExecutorResultError("反馈事件的活动定义不一致")
            if payload.get("mode", current.mode.value) != current.mode.value:
                raise InvalidExecutorResultError("反馈事件的执行模式不一致")
            if "revision" in payload:
                revision = payload["revision"]
                if type(revision) is not int or revision != current.revision:
                    raise InvalidExecutorResultError("反馈事件的活动修订号不一致")
            if current.mode == ExecutionMode.SIMULATED and event.category in {
                EventCategory.ACTIVITY, EventCategory.OBSERVATION,
            }:
                if event.provenance != EventProvenance.SIMULATED:
                    raise InvalidExecutorResultError("模拟活动反馈必须保留 simulated 来源")

    async def _action(self, operation: str, context: ActivityContext) -> ActivityExecutionResult:
        result = await self._invoke(operation, context)
        if not isinstance(result, ActivityExecutionResult):
            raise InvalidExecutorResultError("活动操作必须返回 ActivityExecutionResult")
        return result

    async def start(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("start", context)

    async def step(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("step", context)

    async def observe(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("observe", context)

    async def pause(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("pause", context)

    async def resume(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("resume", context)

    async def stop(self, context: ActivityContext) -> ActivityExecutionResult:
        return await self._action("stop", context)

    async def snapshot(self, context: ActivityContext) -> ActivityInstance:
        result = await self._invoke("snapshot", context)
        if not isinstance(result, ActivityInstance):
            raise InvalidExecutorResultError("snapshot 必须返回 ActivityInstance")
        return result
