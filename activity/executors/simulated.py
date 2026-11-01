"""时间驱动的模拟执行器，返回状态与事件，不访问数据库或模型。"""

from __future__ import annotations

import json
import math
from uuid import NAMESPACE_URL, uuid5

from pydantic import JsonValue

from ..executor import (
    ActivityContext, ActivityExecutionError, ActivityExecutionResult,
    ExecutorCapabilities, InvalidActivityStatusError,
)
from ..models import ActivityInstance, ActivityStatus, ExecutionMode
from ...core.events import AgentEvent, EventProvenance


_TERMINAL_STATUSES = {ActivityStatus.COMPLETED, ActivityStatus.FAILED, ActivityStatus.CANCELLED}


class SimulatedActivityExecutor:
    """无内部进度缓存；每次使用调用方传入的最新实例继续计算。

    当前仅支持有明确预计时长的模拟活动。一次活动需沿用相同的定义，
    暂停或恢复时保留进度，通过 updated_at 区分运行时间与暂停时间。
    真实或混合模式不能由此执行器替代。同一输入重试会返回相同事件 ID。
    """

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            supports_pause=True, supports_resume=True, supports_snapshot=True,
        )

    def _validate(self, context: ActivityContext) -> ActivityContext:
        context = ActivityContext.model_validate(context.model_dump())
        if context.instance.mode != ExecutionMode.SIMULATED:
            raise ActivityExecutionError("模拟执行器只支持 simulated 模式")
        if context.definition.estimated_duration_seconds is None:
            raise ActivityExecutionError("时间驱动的模拟执行器需要明确活动时长")
        if context.instance.status in (ActivityStatus.RUNNING, ActivityStatus.PAUSED):
            if context.instance.progress is None:
                raise ActivityExecutionError("模拟活动运行或暂停时必须有明确进度")
        return context

    def _require_status(self, context: ActivityContext, expected: ActivityStatus) -> None:
        if context.instance.status != expected:
            raise InvalidActivityStatusError(f"当前操作要求活动状态为 {expected.value}")

    def _progress(self, context: ActivityContext) -> float:
        progress = context.instance.progress
        duration = context.definition.estimated_duration_seconds
        # 仅由已校验的运行中上下文调用。
        if progress is None or duration is None:
            raise ActivityExecutionError("推进模拟活动需要进度与时长")
        elapsed = (context.now - context.instance.updated_at).total_seconds()
        remaining = (1.0 - progress) * duration
        tolerance = min(1e-9, 4 * math.ulp(duration))
        if elapsed >= remaining or (
            elapsed > 0 and math.isclose(elapsed, remaining, rel_tol=0.0, abs_tol=tolerance)
        ):
            return 1.0
        return min(1.0, progress + elapsed / duration)

    def _result(
        self, context: ActivityContext, status: ActivityStatus, progress: float, event_type: str
    ) -> ActivityExecutionResult:
        instance = ActivityInstance(**{
            **context.instance.model_dump(),
            "status": status,
            "progress": progress,
            "updated_at": context.now,
            "revision": context.instance.revision + 1,
        })
        identity = json.dumps(
            ["persona.activity", instance.agent_id, instance.activity_id, instance.revision, event_type],
            ensure_ascii=False, separators=(",", ":"),
        )
        payload: dict[str, JsonValue] = {
            "agent_id": instance.agent_id,
            "definition_id": instance.definition_id,
            "mode": instance.mode.value,
            "status": instance.status.value,
            "progress": instance.progress,
            "revision": instance.revision,
        }
        effect = context.definition.completion_needs_delta
        if status == ActivityStatus.COMPLETED and effect is not None:
            payload["needs_delta"] = effect.model_dump(mode="json")
        event = AgentEvent(
            event_id=str(uuid5(NAMESPACE_URL, identity)),
            event_type=event_type,
            timestamp=context.now,
            source="activity.simulated",
            payload=payload,
            activity_id=instance.activity_id,
            correlation_id=instance.activity_id,
            provenance=EventProvenance.SIMULATED,
        )
        return ActivityExecutionResult(instance=instance, events=(event,))

    async def start(self, context: ActivityContext) -> ActivityExecutionResult:
        context = self._validate(context)
        self._require_status(context, ActivityStatus.PENDING)
        return self._result(context, ActivityStatus.RUNNING, 0.0, "ACTIVITY.started")

    async def step(self, context: ActivityContext) -> ActivityExecutionResult:
        context = self._validate(context)
        self._require_status(context, ActivityStatus.RUNNING)
        progress = self._progress(context)
        if progress >= 1.0:
            return self._result(context, ActivityStatus.COMPLETED, 1.0, "ACTIVITY.completed")
        if progress == context.instance.progress:
            return ActivityExecutionResult(instance=context.instance)
        return self._result(context, ActivityStatus.RUNNING, progress, "ACTIVITY.progressed")

    async def observe(self, context: ActivityContext) -> ActivityExecutionResult:
        """读取当前模拟状态，不推进时间，也不生成新的经历。"""
        context = self._validate(context)
        return ActivityExecutionResult(instance=context.instance)

    async def pause(self, context: ActivityContext) -> ActivityExecutionResult:
        context = self._validate(context)
        self._require_status(context, ActivityStatus.RUNNING)
        progress = self._progress(context)
        if progress >= 1.0:
            return self._result(context, ActivityStatus.COMPLETED, 1.0, "ACTIVITY.completed")
        return self._result(context, ActivityStatus.PAUSED, progress, "ACTIVITY.paused")

    async def resume(self, context: ActivityContext) -> ActivityExecutionResult:
        context = self._validate(context)
        self._require_status(context, ActivityStatus.PAUSED)
        progress = context.instance.progress
        if progress is None:
            raise ActivityExecutionError("恢复模拟活动需要明确进度")
        return self._result(
            context, ActivityStatus.RUNNING, progress, "ACTIVITY.resumed",
        )

    async def stop(self, context: ActivityContext) -> ActivityExecutionResult:
        context = self._validate(context)
        if context.instance.status in _TERMINAL_STATUSES:
            return ActivityExecutionResult(instance=context.instance)
        progress = context.instance.progress if context.instance.progress is not None else 0.0
        if context.instance.status == ActivityStatus.RUNNING:
            progress = self._progress(context)
            if progress >= 1.0:
                return self._result(context, ActivityStatus.COMPLETED, 1.0, "ACTIVITY.completed")
        return self._result(context, ActivityStatus.CANCELLED, progress, "ACTIVITY.cancelled")

    async def snapshot(self, context: ActivityContext) -> ActivityInstance:
        """返回可序列化实例；恢复时仍需原定义，不包含数据库保存操作。"""
        return self._validate(context).instance
